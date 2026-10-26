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

import pytest

from scripts import musubi_claude_binding as binding
from tests.test_stop_envelope import SESSION, Tx, _load_stop_module

OPTION = "CLAUDE_PLUGIN_OPTION_"
PROMPT_ID = "p-123"  # Tx.typed's own promptId: the anchor must resolve, or a control proves nothing


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
        tmp_path / "plugin-data",
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


def degraded(tmp_path: Path) -> list[str]:
    path = tmp_path / "plugin-data" / "degraded.jsonl"
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
    path = binding.binding_path(tmp_path / "plugin-data", SESSION)
    row = json.loads(path.read_text())
    assert set(row) == {"session_id", "actor", "presence", "zone", "transport_source", "at"}
    assert path.stat().st_mode & 0o077 == 0  # owner-only, like the outbox beside it


def test_a_binding_for_another_session_is_not_read(tmp_path: Path) -> None:
    bind_as(tmp_path, "yua")
    assert binding.read(tmp_path / "plugin-data", "some-other-session") is None


def test_expired_bindings_are_pruned(tmp_path: Path) -> None:
    root = tmp_path / "plugin-data"
    bind_as(tmp_path, "yua", age=binding.MAX_AGE_SECONDS + 60)
    stale = binding.binding_path(root, SESSION)
    import os

    old = time.time() - binding.MAX_AGE_SECONDS - 60
    os.utime(stale, (old, old))
    assert binding.prune(root) == 1
    assert not stale.exists()
