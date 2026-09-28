"""Tests for the Stop-hook envelope projection.

These tests exercise `scripts.musubi-claude-stop` directly, which is the
piece of Claude-specific logic the harness doesn't know about. The harness
validates the envelope once it arrives; this adapter is responsible for
*producing* one from a Claude Stop event + transcript.

The tests here are intentionally focused on the things that have caused
production defects:
- Event id is the EXCHANGE identity (`exchange.v1:claude-code:<session>:<answer message.id>`),
  per musubi-harness docs/exchange-identity.md, so retries and replays dedupe.
- The Stop event's `last_assistant_message` is authoritative for the answer TEXT; the
  transcript supplies the anchor id and the input span, and must agree on the text.
- An answer not yet written at Stop time is deferred, never dropped, and resolves later.
- The hook degrades visibly and exits 0 on every failure.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

# Locate the stop-hook script in the plugin's scripts/ directory. It's a
# standalone .py file with a shebang, not importable as a normal module.
PLUGIN_ROOT = Path(__file__).resolve().parents[1]
STOP_SCRIPT = PLUGIN_ROOT / "scripts" / "musubi-claude-stop"


def _load_stop_module() -> Any:
    """Import the stop script as `scripts.musubi_claude_stop`.

    The script has a shebang (`#!/usr/bin/env python3`) at the top.
    `importlib.util.spec_from_file_location` returns None for files
    starting with a shebang, so we strip the first line before loading.

    The script imports its sibling `musubi_claude_runtime` from the
    same directory; in production that's resolved because Claude Code
    invokes the script with `scripts/` on `sys.path`. We replicate that
    here by inserting the directory into `sys.path` before exec.
    """
    text = STOP_SCRIPT.read_text(encoding="utf-8")
    if text.startswith("#!"):
        text = text.split("\n", 1)[1]
    scripts_dir = str(STOP_SCRIPT.parent)
    sys.path.insert(0, scripts_dir)
    try:
        spec = importlib.util.spec_from_loader(
            "scripts.musubi_claude_stop",
            loader=None,
            origin=str(STOP_SCRIPT),
        )
        assert spec is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        exec(compile(text, str(STOP_SCRIPT), "exec"), module.__dict__)
    finally:
        if sys.path and sys.path[0] == scripts_dir:
            sys.path.pop(0)
    return module


@pytest.fixture
def stop_module(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    """Load the stop script with a fake identity and a per-test plugin-data dir."""
    monkeypatch.setenv("MUSUBI_ACTOR", "aoi")
    monkeypatch.setenv("MUSUBI_PRESENCE", "aoi/command-chair")
    monkeypatch.setenv("MUSUBI_ZONE", "home")
    monkeypatch.setenv("PLUGIN_DATA", str(tmp_path / "plugin-data"))
    return _load_stop_module()


SESSION = "s-abc"


class Tx:
    """Claude transcript records in the shapes measured on real seats (2026-09-28)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.records: list[dict[str, Any]] = []
        self._n = 0
        self._last: str | None = None

    def _add(self, record: dict[str, Any]) -> str:
        self._n += 1
        uid = f"u{self._n:03d}"
        record.update({"uuid": uid, "parentUuid": self._last, "sessionId": record.get("sessionId", SESSION)})
        self.records.append(record)
        self._last = uid
        self.flush()
        return uid

    def typed(self, text: str, prompt: str = "p-123", **extra: Any) -> str:
        return self._add(
            {
                "type": "user",
                "promptId": prompt,
                "promptSource": "typed",
                "origin": {"kind": "human"},
                "message": {"role": "user", "content": text},
                **extra,
            }
        )

    def task(self, text: str, prompt: str = "p-123") -> str:
        return self._add(
            {
                "type": "user",
                "promptId": prompt,
                "promptSource": "system",
                "origin": {"kind": "task-notification"},
                "message": {"role": "user", "content": text},
            }
        )

    def answer(self, msg: str, text: str, stop: str = "end_turn") -> str:
        return self._add(
            {
                "type": "assistant",
                "message": {"id": msg, "role": "assistant", "stop_reason": stop, "content": [{"type": "text", "text": text}]},
            }
        )

    def flush(self) -> None:
        self.path.write_text("\n".join(json.dumps(r) for r in self.records) + "\n", encoding="utf-8")


@pytest.fixture
def tx(tmp_path: Path) -> Tx:
    return Tx(tmp_path / "transcript.jsonl")


def hook_for(tx: Tx, answer: str = "world", prompt: str = "p-123", **extra: Any) -> dict[str, Any]:
    return {"transcript_path": str(tx.path), "session_id": SESSION, "prompt_id": prompt, "last_assistant_message": answer, **extra}


@pytest.fixture
def captured(stop_module: Any, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Replace the harness enqueue with a recorder; no subprocess, no outbox."""
    sink: list[dict[str, Any]] = []
    monkeypatch.setattr(stop_module, "_enqueue", lambda envelope, configured: sink.append(envelope))
    # Stage is exercised for real by the delivery tests; here it would run whatever
    # harness binary is on PATH.
    monkeypatch.setattr(stop_module, "_stage", lambda envelope, configured: None)
    monkeypatch.setattr(stop_module, "POLL_ATTEMPTS", 1)
    monkeypatch.setattr(stop_module, "POLL_INTERVAL_SECONDS", 0)
    return sink


def pending_files(stop_module: Any) -> list[Path]:
    root = stop_module._seat_root(stop_module.runtime_config()) / "pending"
    return sorted(root.glob("*.json")) if root.is_dir() else []


# ---------------------------------------------------------------------------
# The Stop event's own facts
# ---------------------------------------------------------------------------


def test_camelcase_aliases_accepted(stop_module: Any, tx: Tx) -> None:
    candidate = stop_module.candidate_from_hook(
        {
            "transcript_path": str(tx.path),
            "session_id": SESSION,
            "promptId": "p-123",
            "lastAssistantMessage": "world",
        }
    )
    assert (candidate.prompt_id, candidate.answer_text) == ("p-123", "world")


@pytest.mark.parametrize(
    ("drop", "reason"),
    [
        ("prompt_id", "hook_prompt_id_missing"),
        ("session_id", "hook_session_missing"),
        ("last_assistant_message", "hook_answer_missing"),
        ("transcript_path", "hook_transcript_missing"),
    ],
)
def test_missing_event_fact_refused(stop_module: Any, tx: Tx, drop: str, reason: str) -> None:
    hook = hook_for(tx)
    del hook[drop]
    with pytest.raises(stop_module.AdapterError, match=reason):
        stop_module.candidate_from_hook(hook)


def test_alias_conflict_refused(stop_module: Any, tx: Tx) -> None:
    with pytest.raises(stop_module.AdapterError, match="hook_prompt_id_conflict"):
        stop_module.candidate_from_hook(hook_for(tx, promptId="p-OTHER"))


def stop_run(stop_module: Any, monkeypatch: pytest.MonkeyPatch, hook: dict[str, Any] | str, *, drain_only: bool = False) -> None:
    import io

    raw = hook if isinstance(hook, str) else json.dumps(hook)
    monkeypatch.setattr(sys, "stdin", io.StringIO(raw))
    assert stop_module.main(["--drain-only"] if drain_only else []) == 0


def degraded_reasons(stop_module: Any) -> list[str]:
    path = stop_module._data_root() / "degraded.jsonl"
    return [json.loads(line)["reason"] for line in path.read_text().splitlines()] if path.exists() else []


# ---------------------------------------------------------------------------
# Envelope projection
# ---------------------------------------------------------------------------


def test_voice_envelope_shape(stop_module: Any, tx: Tx, captured: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch) -> None:
    first = tx.typed("hello")
    tx.answer("msg_A", "world")
    stop_run(stop_module, monkeypatch, hook_for(tx))
    [envelope] = captured
    assert envelope["event_id"] == f"exchange.v1:claude-code:{SESSION}:msg_A"
    assert (envelope["user_text"], envelope["assistant_text"]) == ("hello", "world")
    assert envelope["metadata"]["input_record_ids"] == json.dumps([first], separators=(",", ":"))
    assert envelope["metadata"]["answer_id"] == "msg_A"
    # Voice envelopes carry no trigger keys at all: byte-identical to pre-1.7 voice.
    assert not {"input_kind", "trigger_class", "trigger_record_id", "trigger_text"} & set(envelope)
    # Hook decoration does not reach metadata, so a replay with a different model
    # cannot collide.
    assert "model" not in envelope["metadata"]
    assert pending_files(stop_module) == []


def test_answer_text_is_the_events_verbatim(
    stop_module: Any, tx: Tx, captured: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    text = "  keep\n\n```\ncode\n```  "
    tx.typed("q")
    tx.answer("msg_A", text)
    stop_run(stop_module, monkeypatch, hook_for(tx, answer=text))
    assert captured[0]["assistant_text"] == text


def test_transcript_disagreeing_with_event_text_declines(
    stop_module: Any, tx: Tx, captured: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    tx.typed("q")
    tx.answer("msg_A", "what the transcript says")
    stop_run(stop_module, monkeypatch, hook_for(tx, answer="what Stop says"))
    assert captured == [] and pending_files(stop_module) == []
    reasons = degraded_reasons(stop_module)
    assert reasons == ["answer_text_mismatch"]
    assert "what Stop says" not in (stop_module._data_root() / "degraded.jsonl").read_text()


def test_trigger_envelope_shape(stop_module: Any, tx: Tx, captured: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch) -> None:
    rid = tx.task("agent finished")
    tx.answer("msg_A", "world")
    stop_run(stop_module, monkeypatch, hook_for(tx))
    [envelope] = captured
    assert envelope["user_text"] == ""
    assert (envelope["input_kind"], envelope["trigger_class"], envelope["trigger_record_id"], envelope["trigger_text"]) == (
        "trigger",
        "task-notification",
        rid,
        "agent finished",
    )


def test_transcript_session_mismatch_declines(
    stop_module: Any, tx: Tx, captured: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    tx.typed("q")
    tx.answer("msg_A", "world")
    stop_run(stop_module, monkeypatch, {**hook_for(tx), "session_id": "s-OTHER"})
    assert captured == [] and pending_files(stop_module) == []
    assert degraded_reasons(stop_module) == ["hook_session_transcript_mismatch"]


# ---------------------------------------------------------------------------
# The write race: save first, resolve when the answer lands
# ---------------------------------------------------------------------------


def test_answer_not_yet_written_is_deferred_not_dropped(
    stop_module: Any, tx: Tx, captured: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    tx.typed("q")
    stop_run(stop_module, monkeypatch, hook_for(tx))
    assert captured == []
    [path] = pending_files(stop_module)
    assert (path.stat().st_mode & 0o777) == 0o600
    assert json.loads(path.read_text())["attempts"] == 1


def test_deferred_candidate_resolves_on_drain_only(
    stop_module: Any, tx: Tx, captured: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    tx.typed("q")
    stop_run(stop_module, monkeypatch, hook_for(tx))
    tx.answer("msg_A", "world")
    stop_run(stop_module, monkeypatch, {"session_id": SESSION, "reason": "exit"}, drain_only=True)
    assert [e["metadata"]["answer_id"] for e in captured] == ["msg_A"]
    assert pending_files(stop_module) == []


def test_expired_candidate_is_declined_visibly(
    stop_module: Any, tx: Tx, captured: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("musubi_claude_pending.MAX_ATTEMPTS", 2)
    tx.typed("q")
    stop_run(stop_module, monkeypatch, hook_for(tx))
    stop_run(stop_module, monkeypatch, {}, drain_only=True)
    assert pending_files(stop_module) == []
    assert degraded_reasons(stop_module) == ["pending_expired"]


def test_definitive_decline_removes_candidate(
    stop_module: Any, tx: Tx, captured: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    tx.typed("q")
    stop_run(stop_module, monkeypatch, hook_for(tx))
    tx.answer("msg_A", "not what Stop said")
    stop_run(stop_module, monkeypatch, {}, drain_only=True)
    assert captured == [] and pending_files(stop_module) == []
    assert degraded_reasons(stop_module) == ["answer_text_mismatch"]


# Yua's gate on 9834dbf, one test per defect.


def test_enqueue_failure_keeps_the_current_candidate(stop_module: Any, tx: Tx, monkeypatch: pytest.MonkeyPatch) -> None:
    """(1) The answer is on disk but enqueue fails: the exchange must survive."""
    monkeypatch.setattr(stop_module, "POLL_ATTEMPTS", 1)

    def broken(envelope: dict[str, Any], configured: Any) -> None:
        raise stop_module.AdapterError("shadow_enqueue_failed:exit=1")

    monkeypatch.setattr(stop_module, "_enqueue", broken)
    tx.typed("q")
    tx.answer("msg_A", "world")
    stop_run(stop_module, monkeypatch, hook_for(tx))
    assert len(pending_files(stop_module)) == 1
    assert degraded_reasons(stop_module) == ["pending_retry:shadow_enqueue_failed:exit=1"]


def test_one_enqueue_failure_does_not_block_later_candidates_or_hide_successes(
    stop_module: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(2) A failing older candidate must not stop the ones after it, and the ones
    that succeeded must still reach the remote drain."""
    monkeypatch.setattr(stop_module, "POLL_ATTEMPTS", 1)
    txs = {name: Tx(tmp_path / f"{name}.jsonl") for name in ("a", "b", "c")}
    for name, t in txs.items():
        t.typed(f"q-{name}", prompt=f"p-{name}")
        stop_run(stop_module, monkeypatch, hook_for(t, answer=f"ans-{name}", prompt=f"p-{name}"))
    for name, t in txs.items():
        t.answer(f"msg_{name}", f"ans-{name}")
    enqueued: list[str] = []

    def flaky(envelope: dict[str, Any], configured: Any) -> None:
        if envelope["metadata"]["answer_id"] == "msg_a":
            raise stop_module.AdapterError("shadow_enqueue_failed:exit=1")
        enqueued.append(envelope["metadata"]["answer_id"])

    drained: list[str] = []
    monkeypatch.setattr(stop_module, "_stage", lambda envelope, configured: None)
    monkeypatch.setattr(stop_module, "_enqueue", flaky)
    monkeypatch.setattr(stop_module, "_drain_remote", lambda env, cfg, hook: drained.append(env["metadata"]["answer_id"]))
    stop_run(stop_module, monkeypatch, hook_for(txs["c"], answer="ans-c", prompt="p-c"))
    assert enqueued == ["msg_b", "msg_c"]
    assert drained == ["msg_c"]
    assert len(pending_files(stop_module)) == 1  # msg_a waits for the next pass


def test_unreadable_transcript_is_retried_not_deleted(
    stop_module: Any, tx: Tx, captured: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """(3) A rotated or briefly missing transcript keeps its candidate until expiry."""
    tx.typed("q")
    stop_run(stop_module, monkeypatch, hook_for(tx))
    tx.path.rename(tx.path.with_suffix(".moved"))
    stop_run(stop_module, monkeypatch, {}, drain_only=True)
    assert len(pending_files(stop_module)) == 1
    assert "pending_retry:transcript_unreadable" in degraded_reasons(stop_module)
    tx.path.with_suffix(".moved").rename(tx.path)
    tx.answer("msg_A", "world")
    stop_run(stop_module, monkeypatch, {}, drain_only=True)
    assert [e["metadata"]["answer_id"] for e in captured] == ["msg_A"]


def test_older_candidates_are_captured_before_the_current_one(
    stop_module: Any, tmp_path: Path, captured: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """(4) Chronology: a deferred earlier exchange lands before the Stop that follows it."""
    t = Tx(tmp_path / "t.jsonl")
    t.typed("first", prompt="p-1")
    stop_run(stop_module, monkeypatch, hook_for(t, answer="one", prompt="p-1"))
    t.answer("msg_1", "one")
    t.typed("second", prompt="p-2")
    t.answer("msg_2", "two")
    stop_run(stop_module, monkeypatch, hook_for(t, answer="two", prompt="p-2"))
    assert [e["metadata"]["answer_id"] for e in captured] == ["msg_1", "msg_2"]


def test_drain_only_never_talks_to_musubi(
    stop_module: Any, tx: Tx, captured: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """(5) SessionStart/End have 15 s; the 24 s remote pass is the next Stop's job."""
    calls: list[str] = []
    monkeypatch.setattr(stop_module, "_drain_remote", lambda *a: calls.append("remote"))
    tx.typed("q")
    stop_run(stop_module, monkeypatch, hook_for(tx))
    tx.answer("msg_A", "world")
    stop_run(stop_module, monkeypatch, {}, drain_only=True)
    assert len(captured) == 1 and calls == []


# Yua's gate on 5f003e6: the hook budget.


def test_failing_enqueue_is_not_polled(stop_module: Any, tx: Tx, monkeypatch: pytest.MonkeyPatch) -> None:
    """Only a not-yet-written answer is worth polling. A broken harness is not."""
    calls: list[str] = []
    sleeps: list[float] = []

    def broken(envelope: dict[str, Any], configured: Any) -> None:
        calls.append(envelope["metadata"]["answer_id"])
        raise stop_module.AdapterError("shadow_enqueue_failed:exit=1")

    monkeypatch.setattr(stop_module, "_enqueue", broken)
    monkeypatch.setattr(stop_module.time, "sleep", sleeps.append)
    tx.typed("q")
    tx.answer("msg_A", "world")
    stop_run(stop_module, monkeypatch, hook_for(tx))
    assert calls == ["msg_A"] and sleeps == []
    [path] = pending_files(stop_module)
    # An infrastructure failure does not spend the not-yet attempt budget.
    assert json.loads(path.read_text())["attempts"] == 0


def test_current_candidate_waits_when_the_budget_is_spent(
    stop_module: Any, tx: Tx, captured: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(stop_module, "CURRENT_START_LIMIT_SECONDS", -1.0)
    tx.typed("q")
    tx.answer("msg_A", "world")
    stop_run(stop_module, monkeypatch, hook_for(tx))
    assert captured == [] and len(pending_files(stop_module)) == 1


def test_remote_pass_is_skipped_when_local_work_spent_the_budget(
    stop_module: Any, tx: Tx, captured: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    remote: list[str] = []
    monkeypatch.setattr(stop_module, "_drain_remote", lambda *a: remote.append("remote"))
    monkeypatch.setattr(stop_module, "REMOTE_START_LIMIT_SECONDS", -1.0)
    tx.typed("q")
    tx.answer("msg_A", "world")
    stop_run(stop_module, monkeypatch, hook_for(tx))
    assert len(captured) == 1 and remote == []


def test_drain_only_uses_its_own_tighter_budget(
    stop_module: Any, tx: Tx, captured: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """SessionStart/End have 15 s, so drain-only starts work only inside its own limit."""
    tx.typed("q")
    stop_run(stop_module, monkeypatch, hook_for(tx))
    tx.answer("msg_A", "world")
    monkeypatch.setattr(stop_module, "DRAIN_ONLY_BUDGET_SECONDS", -1.0)
    stop_run(stop_module, monkeypatch, {}, drain_only=True)
    assert captured == [] and len(pending_files(stop_module)) == 1


def test_repeated_stop_keeps_age_and_attempts_but_takes_the_newest_answer(
    stop_module: Any, tx: Tx, captured: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    tx.typed("q")
    stop_run(stop_module, monkeypatch, hook_for(tx, answer="first draft"))
    [path] = pending_files(stop_module)
    before = json.loads(path.read_text())
    stop_run(stop_module, monkeypatch, hook_for(tx, answer="continued final"))
    [path] = pending_files(stop_module)
    after = json.loads(path.read_text())
    assert after["created_at"] == before["created_at"]
    assert after["attempts"] == before["attempts"] + 1
    assert after["answer_text"] == "continued final"


# ---------------------------------------------------------------------------
# main(): never blocks the session
# ---------------------------------------------------------------------------


def test_main_garbage_stdin_degrades(stop_module: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    stop_run(stop_module, monkeypatch, "not json")
    assert degraded_reasons(stop_module) == ["adapter_runtime_failed"]


# ---------------------------------------------------------------------------
# Degraded sink
# ---------------------------------------------------------------------------


def test_degraded_record_has_no_prose(stop_module: Any) -> None:
    """The degraded sink holds only structured failure codes — never prose.

    Verified via the scope helper, which is the only thing that can
    leak conversation content through the diagnostic sink. Its output
    must contain exactly: at, reason, seat (and optionally session_id).
    """
    scope = stop_module._degraded_scope({"session_id": "s-abc"})
    assert set(scope.keys()).issubset({"seat", "session_id"})
    assert scope.get("session_id") == "s-abc"


def test_degraded_scope_handles_non_dict(stop_module: Any) -> None:
    """The helper must not raise even when the hook payload is malformed.

    Seat comes from the runtime config (env or file), not from the
    hook payload, so it's correctly populated even when the payload
    itself is unusable. Session id, however, comes from the hook and
    must be absent in this case.
    """
    scope = stop_module._degraded_scope("not a dict")
    assert scope.get("seat") == "aoi"
    assert "session_id" not in scope


def test_degraded_scope_handles_missing_session(stop_module: Any) -> None:
    """A hook payload with no session_id leaves the session_id key absent."""
    scope = stop_module._degraded_scope({})
    assert "session_id" not in scope
