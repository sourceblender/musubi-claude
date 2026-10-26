"""A turn is captured as the seat that prompted it, or not at all.

Claude Code re-reads the project's ``settings.json`` ``env`` block while a turn runs,
and each hook process gets the value current at its own spawn. Probed 2026-10-26 on
Claude Code 2.1.310 by flipping one variable from inside a running turn: two reads in
that same turn gave the old value and then the new one, no restart. Aoi reproduced it
through a tool call that edited the file, and measured the plugin's own hooks:

    prompt   PROBE_ACTOR=yua
    posttool PROBE_ACTOR=yua
    posttool PROBE_ACTOR=tama
    stop     PROBE_ACTOR=tama

Stop resolves identity from its own environment, so a turn prompted as Yua was
enveloped as Tama. Once a seat can also take its token from its own credential file,
that envelope is also DELIVERABLE under Tama's valid token, and the server's scope
check sees nothing wrong because actor and token agree. Aoi found this on
musubi-claude#48; Tama found the same class on musubi-codex#23.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

# ``musubi_claude_binding`` is imported bare, NOT as ``scripts.musubi_claude_binding``:
# scripts/ is an implicit namespace package on sys.path (see conftest), so the two
# spellings load two distinct module objects, and the hooks import bare. A fixture that
# substitutes fixed_root on the ``scripts.`` copy patches something production never
# loads — the co-flip test below passed with the guard inert until this was fixed.
import musubi_claude_binding as binding
import pytest

from tests.test_stop_envelope import SESSION, Tx, _load_stop_module

OPTION = "CLAUDE_PLUGIN_OPTION_"
PROMPT_ID = "p-123"  # Tx.typed's own promptId: the anchor must resolve, or a control proves nothing

# The real resolver, captured before the fixture below substitutes it. Production has
# no env override on purpose — a knob would re-open the hole the fixed root closes —
# so a test that wants the genuine article has to hold a reference to it.
REAL_FIXED_ROOT = binding.fixed_root


@pytest.fixture(autouse=True)
def binding_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Put the binding under tmp_path, substituting the function rather than an env var.

    Deliberately NOT ``tmp_path / "plugin-data"``: the whole point of the fixed root is
    that it does not move when PLUGIN_DATA does, and a test root living inside the
    plugin data directory would make the co-flip test below pass for the wrong reason.
    """
    root = tmp_path / "binding-root"
    monkeypatch.setattr(binding, "fixed_root", lambda: root)
    return root


def test_the_hooks_and_this_file_hold_the_same_binding_module(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Guards the import above: if these ever diverge again, every substitution in this
    # file silently stops reaching the code under test.
    stop = load_stop(monkeypatch, tmp_path, "yua")
    assert stop.binding is binding


def load_stop(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, actor: str, **env: str) -> Any:
    """The Stop hook, loaded as a seat whose launcher set ``actor``."""
    import os

    for key in list(dict(os.environ)):
        if key.startswith(("MUSUBI_", OPTION)):
            monkeypatch.delenv(key)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "plugin-data"))
    monkeypatch.setenv("MUSUBI_ACTOR", actor)
    monkeypatch.setenv("MUSUBI_PRESENCE", f"{actor}/command-chair")
    monkeypatch.setenv("MUSUBI_ZONE", "home")
    monkeypatch.setenv("MUSUBI_DELIVERY_MODE", "shadow")
    monkeypatch.setenv("MUSUBI_HARNESS_BIN", "/opt/fake/musubi-harness")
    monkeypatch.setenv("MUSUBI_MEMORY_DATA_BIN", "/opt/fake/memory-data")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    sys.modules.pop("musubi_claude_runtime", None)
    return _load_stop_module()


def bind_as(tmp_path: Path, actor: str, *, transport: str = "seat_env_file", age: float = 0.0) -> None:
    """What the UserPromptSubmit hook writes when the turn begins."""
    binding.write(
        binding.fixed_root(),
        binding.Binding(
            session_id=SESSION,
            actor=actor,
            presence=f"{actor}/command-chair",
            zone="home",
            transport_source=transport,
            at=time.time() - age,
        ),
    )


def hook_payload(tmp_path: Path) -> str:
    tx = Tx(tmp_path / "transcript.jsonl")
    tx.typed("hello")
    tx.answer("msg_A", "world")
    return json.dumps(
        {
            "transcript_path": str(tx.path),
            "session_id": SESSION,
            "prompt_id": PROMPT_ID,
            "last_assistant_message": "world",
        }
    )


def run_stop(stop: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[list[str]]:
    """Run the Stop hook with every harness subprocess faked. Returns the argv it ran."""
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "{}", "")

    monkeypatch.setattr(stop.subprocess, "run", fake_run)
    monkeypatch.setattr("sys.stdin", io.StringIO(hook_payload(tmp_path)))
    assert stop.main() == 0
    return calls


def degraded(tmp_path: Path, root: Path | None = None) -> list[str]:
    # root is the plugin DATA directory, which a test may have moved; the binding's own
    # root is somewhere else entirely, which is the property under test.
    path = (root or tmp_path / "plugin-data") / "degraded.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line).get("reason", "") for line in path.read_text().splitlines() if line.strip()]


def subcommands(calls: list[list[str]]) -> set[str]:
    return {part for argv in calls for part in argv if part in {"enqueue", "stage", "drain"}}


def test_a_turn_prompted_as_one_seat_is_not_captured_as_another(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    bind_as(tmp_path, "yua")  # the turn began as Yua
    stop = load_stop(monkeypatch, tmp_path, "tama")  # the setting changed before Stop
    calls = run_stop(stop, monkeypatch, tmp_path)
    assert subcommands(calls) == set(), "nothing may be enqueued or delivered under the second seat"
    assert binding.IDENTITY_CHANGED in degraded(tmp_path)
    # And nothing durable was left behind for a later drain to pick up as Tama.
    assert not list((tmp_path / "plugin-data" / "tama").rglob("pending/*.json"))


def test_the_no_edit_control_still_captures(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Aoi's control: the same path with the setting untouched must behave as before,
    # or the test above is only measuring that captures are broken.
    bind_as(tmp_path, "yua")
    stop = load_stop(monkeypatch, tmp_path, "yua")
    calls = run_stop(stop, monkeypatch, tmp_path)
    assert "enqueue" in subcommands(calls)
    assert binding.IDENTITY_CHANGED not in degraded(tmp_path)


def test_an_absent_binding_captures_exactly_as_before(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # A seat whose UserPromptSubmit never ran or failed must not lose every capture.
    stop = load_stop(monkeypatch, tmp_path, "yua")
    calls = run_stop(stop, monkeypatch, tmp_path)
    assert "enqueue" in subcommands(calls)
    assert binding.IDENTITY_CHANGED not in degraded(tmp_path)


def test_a_stale_binding_is_not_compared(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # A resumed session that never re-prompted: older than MAX_AGE, so unbound.
    bind_as(tmp_path, "yua", age=binding.MAX_AGE_SECONDS + 60)
    stop = load_stop(monkeypatch, tmp_path, "tama")
    calls = run_stop(stop, monkeypatch, tmp_path)
    assert "enqueue" in subcommands(calls)
    assert binding.IDENTITY_CHANGED not in degraded(tmp_path)


def test_a_transport_change_alone_is_recorded_but_does_not_decline(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # The envelope is still correct and the benign direction is common (a launcher
    # export appearing mid-session). Declining it would drop good captures.
    bind_as(tmp_path, "yua", transport="environment")
    stop = load_stop(monkeypatch, tmp_path, "yua")
    calls = run_stop(stop, monkeypatch, tmp_path)
    assert "enqueue" in subcommands(calls)
    assert binding.TRANSPORT_CHANGED in degraded(tmp_path)


def test_an_earlier_pending_candidate_is_not_declined_by_a_newer_binding(tmp_path: Path) -> None:
    # The binding holds the session's LATEST prompt. A candidate from prompt 1 drained
    # at prompt 5's Stop must not be judged against prompt 5 — it would mismatch for the
    # wrong reason. It does not need to be: it sits under its own actor's directory.
    from scripts import musubi_claude_pending as pending

    seat_root = tmp_path / "plugin-data" / "yua" / "home"
    candidate = pending.Candidate(
        session_id=SESSION,
        prompt_id="p-earlier",
        transcript_path=str(tmp_path / "t.jsonl"),
        answer_text="earlier",
        is_sidechain_hint=False,
        created_at=time.time(),
    )
    pending.save(seat_root, candidate)
    assert pending.load(seat_root, candidate.key) is not None
    # Nothing a drain as Tama can reach: the path itself carries the actor.
    assert not list((tmp_path / "plugin-data" / "tama").rglob("pending/*.json"))


def test_the_binding_holds_no_content_and_no_credential(tmp_path: Path) -> None:
    bind_as(tmp_path, "yua")
    path = binding.binding_path(binding.fixed_root(), SESSION)
    row = json.loads(path.read_text())
    assert set(row) == {"session_id", "actor", "presence", "zone", "transport_source", "at"}
    assert path.stat().st_mode & 0o077 == 0  # owner-only, like the outbox beside it


def test_a_binding_for_another_session_is_not_read(tmp_path: Path) -> None:
    bind_as(tmp_path, "yua")
    assert binding.read(binding.fixed_root(), "some-other-session") is None


def test_expired_bindings_are_pruned(tmp_path: Path) -> None:
    root = binding.fixed_root()
    bind_as(tmp_path, "yua", age=binding.MAX_AGE_SECONDS + 60)
    stale = binding.binding_path(root, SESSION)
    import os

    old = time.time() - binding.MAX_AGE_SECONDS - 60
    os.utime(stale, (old, old))
    assert binding.prune(root) == 1
    assert not stale.exists()


def test_moving_the_data_directory_with_the_actor_does_not_evade_the_check(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Aoi's co-flip, the fourth control: flip MUSUBI_ACTOR *and* PLUGIN_DATA together.

    The first version of this guard kept the binding under ``data_root()``, which the
    harness resolves from ``PLUGIN_DATA`` — the same project ``env`` block, re-read
    mid-turn the same way. She probed it with these helpers: bound Yua, Stop as Tama
    with PLUGIN_DATA pointing elsewhere, and the capture enqueued into
    ``elsewhere/tama/home/shadow.db`` because Stop looked in the new folder, found
    nothing, and "absent means proceed" let it through. The outbox is under the new
    root too, so the drain would deliver it with Tama's file token.
    """
    elsewhere = tmp_path / "elsewhere"
    bind_as(tmp_path, "yua")
    stop = load_stop(monkeypatch, tmp_path, "tama", CLAUDE_PLUGIN_DATA=str(elsewhere))
    calls = run_stop(stop, monkeypatch, tmp_path)
    assert subcommands(calls) == set(), "the moved data directory must not buy a capture"
    assert binding.IDENTITY_CHANGED in degraded(tmp_path, elsewhere)
    assert not list(elsewhere.rglob("pending/*.json"))
    assert not list(elsewhere.rglob("*.db"))


def test_the_co_flip_control_still_captures(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Its own no-edit control: moving PLUGIN_DATA alone, with the actor untouched, is
    # not an attack and must still capture — otherwise the test above would pass on a
    # plugin that simply declines whenever the data directory is unfamiliar.
    elsewhere = tmp_path / "elsewhere"
    bind_as(tmp_path, "yua")
    stop = load_stop(monkeypatch, tmp_path, "yua", CLAUDE_PLUGIN_DATA=str(elsewhere))
    calls = run_stop(stop, monkeypatch, tmp_path)
    assert "enqueue" in subcommands(calls)
    assert binding.IDENTITY_CHANGED not in degraded(tmp_path, elsewhere)


def test_the_fixed_root_is_not_reachable_from_the_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Every variable a project's env block could use to redirect it, at once.
    before = REAL_FIXED_ROOT()
    for key in ("HOME", "CLAUDE_PLUGIN_DATA", "PLUGIN_DATA", "XDG_STATE_HOME", "MUSUBI_ACTOR"):
        monkeypatch.setenv(key, str(tmp_path / "attacker"))
    assert REAL_FIXED_ROOT() == before
    assert str(tmp_path) not in str(before)


def test_the_fixed_root_is_the_os_user_home(tmp_path: Path) -> None:
    # Stated so a later refactor to Path.home() (which reads $HOME) fails here.
    import os
    import pwd

    expected = Path(pwd.getpwuid(os.getuid()).pw_dir) / ".local" / "state" / "musubi-claude"
    assert REAL_FIXED_ROOT() == expected
