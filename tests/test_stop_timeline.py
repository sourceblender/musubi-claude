"""One metadata-only line per Stop, and the hook behaves the same with or without it.

The line exists to say whether a backlog comes from a pass that never started or a pass
that failed. So each category is driven through the real hook path, and each cell checks
the drain the hook actually ran beside what the line claims.
"""

from __future__ import annotations

import io
import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tests.test_credential_boundary import OPTION, SETTINGS, hook_payload, load_stop
from tests.test_stop_envelope import SESSION, Tx

ANSWER = "world"
KEYS = {
    "at",
    "current_polls",
    "current_skipped",
    "exit",
    "gate_at_s",
    "gate_left_s",
    "load_s",
    "loads",
    "mode",
    "older",
    "older_started",
    "outcomes",
    "pass",
    "pass_s",
    "pending",
    "rows_attempted",
    "rows_verified",
    "seat",
    "session_id",
    "stage_s",
    "stages",
    "total_s",
}
ENV = {
    **SETTINGS,
    OPTION + "DELIVERY_MODE": "verified",
    "MUSUBI_HARNESS_BIN": "/opt/fake/musubi-harness",
    "MUSUBI_MEMORY_DATA_BIN": "/opt/fake/memory-data",
}


def rows(*states: str) -> str:
    results = [{"state": state} for state in states]
    return json.dumps({"ok": True, "result": results[0], "results": results})


def completed(stdout: str, code: int = 0, stderr: str = "") -> Callable[[list[str]], subprocess.CompletedProcess[str]]:
    return lambda argv: subprocess.CompletedProcess(argv, code, stdout, stderr)


def run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    drain: Callable[[list[str]], subprocess.CompletedProcess[str]],
    *,
    payload: str | None = None,
    args: list[str] | None = None,
    prepare: Callable[[Any], None] | None = None,
    env: dict[str, str] | None = None,
) -> tuple[Any, list[list[str]]]:
    stop = load_stop(monkeypatch, tmp_path, **(ENV if env is None else env))
    drains: list[list[str]] = []

    def fake_run(argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        if argv[3] != "drain":
            return subprocess.CompletedProcess(argv, 0, "{}", "")
        drains.append(argv)
        return drain(argv)

    monkeypatch.setattr(stop.subprocess, "run", fake_run)
    if prepare is not None:
        prepare(stop)
    monkeypatch.setattr("sys.stdin", io.StringIO(hook_payload(tmp_path) if payload is None else payload))
    assert stop.main([] if args is None else args) == 0
    return stop, drains


def lines(tmp_path: Path) -> list[dict[str, Any]]:
    path = tmp_path / "plugin-data" / "stop-timeline.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


def line(tmp_path: Path) -> dict[str, Any]:
    found = lines(tmp_path)
    assert len(found) == 1
    return found[0]


def unanswered(tmp_path: Path) -> str:
    tmp_path.mkdir(exist_ok=True)
    tx = Tx(tmp_path / "transcript.jsonl")
    tx.typed("hello")
    return json.dumps({"transcript_path": str(tx.path), "session_id": SESSION, "prompt_id": "p-123", "last_assistant_message": ANSWER})


def test_a_delivered_pass(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _, drains = run(monkeypatch, tmp_path, completed(rows("verified", "verified", "verified")))
    record = line(tmp_path)
    assert len(drains) == 1
    assert set(record) == KEYS
    assert record["pass"] == "delivered"
    assert (record["rows_attempted"], record["rows_verified"]) == (3, 3)
    assert (record["loads"], record["stages"], record["current_polls"]) == (1, 1, 1)
    assert record["outcomes"] == {"resolved": 1}
    assert record["gate_left_s"] == pytest.approx(4.0 - record["gate_at_s"], abs=0.002)
    assert record["pass_s"] is not None
    assert record["total_s"] >= record["gate_at_s"]
    assert (record["seat"], record["session_id"], record["exit"], record["mode"]) == ("aoi", SESSION, "completed", "stop")


def test_the_start_limit_skip_is_named_and_no_drain_runs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def past_the_limit(stop: Any) -> None:
        stop.REMOTE_START_LIMIT_SECONDS = -1.0

    _, drains = run(monkeypatch, tmp_path, completed(rows("verified")), prepare=past_the_limit)
    record = line(tmp_path)
    assert drains == []
    assert record["pass"] == "skip_start_limit"
    assert record["gate_left_s"] < 0
    assert record["pass_s"] is None
    assert record["outcomes"] == {"resolved": 1}


def test_a_turn_that_resolves_nothing_never_reaches_the_drain(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def no_sleep(stop: Any) -> None:
        stop.POLL_INTERVAL_SECONDS = 0.0

    _, drains = run(monkeypatch, tmp_path, completed(rows("verified")), payload=unanswered(tmp_path), prepare=no_sleep)
    record = line(tmp_path)
    assert drains == []
    assert record["pass"] == "skip_nothing_captured"
    assert record["outcomes"] == {"not_yet": 1}


def test_loads_count_each_real_read_and_the_line_adds_none(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    reads: list[str] = []
    original = Path.read_text

    def counting(self: Path, *args: Any, **kwargs: Any) -> str:
        if self.name == "transcript.jsonl":
            reads.append(self.name)
        return original(self, *args, **kwargs)

    def prepare(stop: Any) -> None:
        stop.POLL_INTERVAL_SECONDS = 0.0
        monkeypatch.setattr(Path, "read_text", counting)

    payload = unanswered(tmp_path)
    run(monkeypatch, tmp_path, completed(rows("verified")), payload=payload, prepare=prepare)
    record = line(tmp_path)
    assert record["current_polls"] == 5
    assert record["loads"] == len(reads) == 5


def test_an_unreadable_transcript_still_counts_the_attempted_load(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    payload = json.dumps(
        {"transcript_path": str(tmp_path / "gone.jsonl"), "session_id": SESSION, "prompt_id": "p-123", "last_assistant_message": ANSWER}
    )
    run(monkeypatch, tmp_path, completed(rows("verified")), payload=payload)
    record = line(tmp_path)
    assert record["loads"] == 1
    assert record["outcomes"] == {"retry": 1}
    assert record["pass"] == "skip_nothing_captured"


def test_an_older_candidate_is_counted_and_costs_its_own_load(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def no_sleep(stop: Any) -> None:
        stop.POLL_INTERVAL_SECONDS = 0.0

    older = json.loads(unanswered(tmp_path / "older"))
    older["prompt_id"] = "p-older"
    stop, drains = run(monkeypatch, tmp_path, completed(rows("verified")), payload=json.dumps(older), prepare=no_sleep)
    monkeypatch.setattr("sys.stdin", io.StringIO(hook_payload(tmp_path)))
    assert stop.main([]) == 0
    first, second = lines(tmp_path)
    assert first["pending"] == 1 and first["older"] == 0
    assert (second["pending"], second["older"], second["older_started"]) == (2, 1, 1)
    assert second["loads"] == 2
    assert second["outcomes"] == {"not_yet": 1, "resolved": 1}
    assert second["pass"] == "delivered" and len(drains) == 1


def test_drain_only_stays_off_the_network(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _, drains = run(monkeypatch, tmp_path, completed(rows("verified")), payload="{}", args=["--drain-only"])
    record = line(tmp_path)
    assert drains == []
    assert (record["mode"], record["pass"], record["loads"]) == ("drain_only", "skip_drain_only", 0)


def test_shadow_mode_has_no_remote_pass(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    env = {**ENV, OPTION + "DELIVERY_MODE": "shadow"}
    _, drains = run(monkeypatch, tmp_path, completed(rows("verified")), env=env)
    record = line(tmp_path)
    assert drains == []
    assert (record["pass"], record["exit"], record["pass_s"]) == ("no_remote", "completed", None)


@pytest.mark.parametrize(
    ("stdout", "category", "attempted", "verified"),
    [
        (rows("idle"), "idle", 0, 0),
        (rows("verified", "idle"), "delivered", 1, 1),
        (rows("verified", "pending"), "partial", 2, 1),
        (rows("verified", "error"), "partial", 2, 1),
        (rows("pending"), "stalled", 1, 0),
        (rows("accepted"), "stalled", 1, 0),
        (json.dumps({"ok": True, "result": {"state": "verified"}}), "delivered", 1, 1),
        (json.dumps({"ok": True, "result": {"state": "idle"}}), "idle", 0, 0),
        ("not json", "unknown", 0, 0),
        ("[1, 2]", "unknown", 0, 0),
        (json.dumps({"ok": True}), "unknown", 0, 0),
    ],
    ids=[
        "idle",
        "idle-after-progress",
        "partial",
        "error-after-progress",
        "stalled",
        "accepted-is-not-verified",
        "one-row-harness",
        "one-row-idle",
        "garbage",
        "non-dict",
        "no-rows",
    ],
)
def test_a_finished_pass_is_classified_from_its_rows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stdout: str, category: str, attempted: int, verified: int
) -> None:
    run(monkeypatch, tmp_path, completed(stdout))
    record = line(tmp_path)
    assert (record["pass"], record["rows_attempted"], record["rows_verified"]) == (category, attempted, verified)
    assert record["exit"] == "completed"


def test_a_remote_failure_keeps_no_raw_text(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    raw = "Server said: Bearer eyJhbGciOi at /Users/someone/secret"
    run(monkeypatch, tmp_path, completed(json.dumps({"ok": False, "error": raw}), 2, raw))
    record = line(tmp_path)
    assert (record["pass"], record["exit"]) == ("remote_failed", "handled_failure")
    assert record["pass_s"] is not None
    assert "eyJ" not in json.dumps(record) and "secret" not in json.dumps(record)


@pytest.mark.parametrize("where", ["stderr", "stdout"])
def test_lock_contention_is_named_and_its_text_is_not_kept(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, where: str) -> None:
    raw = 'File "/Users/someone/delivery.py", sqlite3.OperationalError: database is locked'
    drain = completed(raw, 1, "") if where == "stdout" else completed("", 1, raw)
    run(monkeypatch, tmp_path, drain)
    record = line(tmp_path)
    assert record["pass"] == "lock_contention"
    assert "someone" not in json.dumps(record)


def test_a_pass_that_times_out_is_not_a_skip(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def hangs(argv: list[str]) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(argv, 24)

    run(monkeypatch, tmp_path, hangs)
    record = line(tmp_path)
    assert (record["pass"], record["exit"]) == ("remote_timeout", "handled_failure")
    assert record["pass_s"] is not None


def test_a_pass_that_cannot_start_is_unknown_not_a_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def missing(argv: list[str]) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError(argv[0])

    run(monkeypatch, tmp_path, missing)
    record = line(tmp_path)
    assert (record["pass"], record["exit"]) == ("unknown", "handled_failure")


def test_an_unexpected_failure_is_a_line_and_not_a_lost_hook(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def prepare(stop: Any) -> None:
        def boom(*_: Any, **__: Any) -> None:
            raise ValueError("a transcript fragment")

        stop.drain = boom

    run(monkeypatch, tmp_path, completed(rows("verified")), prepare=prepare)
    record = line(tmp_path)
    assert (record["pass"], record["exit"]) == ("not_reached", "unexpected_failure")
    assert record["gate_at_s"] is None
    assert "fragment" not in json.dumps(record)


def test_a_failed_diagnostic_write_changes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def blocked(stop: Any) -> None:
        (tmp_path / "plugin-data" / "stop-timeline.jsonl").mkdir(parents=True)

    _, drains = run(monkeypatch, tmp_path, completed(rows("verified")), prepare=blocked)
    assert len(drains) == 1
    assert capsys.readouterr().out.strip() == "{}"
    assert not (tmp_path / "plugin-data" / "degraded.jsonl").exists()


def test_a_diagnostic_that_raises_changes_nothing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def raises(*_: Any) -> None:
        raise RuntimeError("the sink itself")

    def prepare(stop: Any) -> None:
        monkeypatch.setattr(stop.timeline.Timeline, "write", raises)

    _, drains = run(monkeypatch, tmp_path, completed(rows("verified")), prepare=prepare)
    assert len(drains) == 1


def test_the_hook_output_is_only_the_empty_object(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run(monkeypatch, tmp_path, completed(rows("verified")))
    assert capsys.readouterr().out.strip() == "{}"


def test_no_conversation_text_or_path_reaches_the_sink(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    run(monkeypatch, tmp_path, completed(rows("verified")))
    raw = (tmp_path / "plugin-data" / "stop-timeline.jsonl").read_text()
    assert ANSWER not in raw and "hello" not in raw
    assert str(tmp_path) not in raw and "/" not in raw


def test_the_sink_is_private_and_stays_bounded(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    stop = load_stop(monkeypatch, tmp_path, **ENV)
    root = tmp_path / "plugin-data"
    root.mkdir(parents=True)
    path = root / stop.timeline.FILENAME
    timeline = stop.timeline.Timeline()
    for round_ in range(3):
        path.write_bytes(b"x" * (stop.timeline.MAX_BYTES + round_))
        timeline.write(root, "aoi", SESSION)
        assert len(path.read_text().splitlines()) == 1
        assert path.with_name(path.name + ".1").stat().st_size == stop.timeline.MAX_BYTES + round_
    assert sorted(p.name for p in root.iterdir()) == ["stop-timeline.jsonl", "stop-timeline.jsonl.1"]
    path.unlink()
    timeline.write(root, "aoi", SESSION)
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("value", ["", "../etc/passwd", "/Users/someone", "has space", "x" * 65, None, 7])
def test_an_identifier_that_is_not_code_shaped_is_dropped(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, value: Any) -> None:
    stop = load_stop(monkeypatch, tmp_path, **ENV)
    record = stop.timeline.Timeline().record(value, value)
    assert "seat" not in record and "session_id" not in record


@pytest.mark.parametrize("drain_only", [False, True])
@pytest.mark.parametrize("captured", [False, True])
@pytest.mark.parametrize("elapsed", [0.0, 3.999, 4.0, 4.001, 30.0])
def test_the_gate_is_the_hooks_condition_exactly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, drain_only: bool, captured: bool, elapsed: float
) -> None:
    stop = load_stop(monkeypatch, tmp_path, **ENV)
    timeline = stop.timeline.Timeline()
    timeline.begin(0.0, drain_only=drain_only)
    entered = timeline.note_gate(elapsed, stop.REMOTE_START_LIMIT_SECONDS, captured=captured)
    assert entered == (captured and not drain_only and elapsed <= stop.REMOTE_START_LIMIT_SECONDS)


def test_a_new_stop_starts_a_clean_line(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    stop = load_stop(monkeypatch, tmp_path, **ENV)
    timeline = stop.timeline.Timeline()
    timeline.note_load(9.0)
    timeline.note_stage(9.0)
    timeline.note_drain(rows("verified"))
    timeline.begin(0.0, drain_only=False)
    record = timeline.record("aoi", SESSION)
    assert (record["loads"], record["stages"], record["rows_verified"], record["pass"]) == (0, 0, 0, "not_reached")
