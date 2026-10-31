"""One metadata-only line per Stop: where the hook's time went and what the remote pass did.

The outbox fell behind on a 477 MB transcript and nothing recorded why: a pass the start
limit skips is silent by design. This module exists to answer that from a measurement.

It only observes. Nothing here may change what the hook does, so every method that runs
on a hook path is plain arithmetic or guarded, and the hook's one call to `write` swallows
any failure.

What a line can say, and what it cannot:

- Every time is seconds on the monotonic clock from `main()` starting, the same origin the
  hook's own limits are measured from. Interpreter start-up before that is not included.
- A hard kill (the 30 s hook timeout, SIGKILL) writes nothing, because the line is written
  last. A missing line is therefore NOT evidence of a skipped pass. Only `skip_start_limit`
  is.
- `idle` means the drainer found nothing it could lease. That is no pending rows, or rows
  another drainer holds: the harness does not say which.
- The sink never holds a body, a path, a credential or raw error text. `pass` and `exit`
  are the fixed categories below, and the two identifiers are dropped unless code-shaped.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

FILENAME = "stop-timeline.jsonl"
# degraded.jsonl and recall.jsonl have no retention to follow, so this sink carries its own:
# one rollover to `.1`, so the pair stays near 2 MiB however long a machine runs.
MAX_BYTES = 1024 * 1024
_LOCKED = "database is locked"
_IDENTIFIER = re.compile(r"[A-Za-z0-9_.:-]{1,64}")

# `pass`: why the remote drain did or did not run, as far as the hook can see.
SKIP_DRAIN_ONLY = "skip_drain_only"  # SessionStart/SessionEnd leave the network to Stop
SKIP_NOTHING_CAPTURED = "skip_nothing_captured"  # the gate needs one resolved candidate
SKIP_START_LIMIT = "skip_start_limit"  # the gate's time limit had passed
NOT_REACHED = "not_reached"  # the hook failed before the gate
ENTERED = "entered"  # replaced when the pass ends; a line that would still say this says `unknown`
NO_REMOTE = "no_remote"  # the gate passed and no remote command ran: shadow mode, or it could not be built
DELIVERED = "delivered"  # every attempted row verified
PARTIAL = "partial"  # some rows verified, then one did not
STALLED = "stalled"  # rows attempted, none verified
IDLE = "idle"  # no leasable row
LOCK_CONTENTION = "lock_contention"
REMOTE_FAILED = "remote_failed"
REMOTE_TIMEOUT = "remote_timeout"
UNKNOWN = "unknown"

# `exit`: how `main()` finished.
EXIT_COMPLETED = "completed"
EXIT_HANDLED_FAILURE = "handled_failure"
EXIT_UNEXPECTED_FAILURE = "unexpected_failure"


class Timeline:
    def __init__(self) -> None:
        self.begin(time.monotonic(), drain_only=False)

    def begin(self, origin: float, *, drain_only: bool) -> None:
        self.origin = origin
        self.drain_only = drain_only
        self.loads = 0
        self.load_s = 0.0
        self.stages = 0
        self.stage_s = 0.0
        self.pending = 0
        self.older = 0
        self.older_started = 0
        self.current_polls = 0
        self.current_skipped = False
        self.outcomes: dict[str, int] = {}
        self.gate_at_s: float | None = None
        self.gate_left_s: float | None = None
        self.pass_state = NOT_REACHED
        self.pass_s: float | None = None
        self.pass_started: float | None = None
        self.rows_attempted = 0
        self.rows_verified = 0
        self.exit = EXIT_COMPLETED

    def note_load(self, seconds: float) -> None:
        self.loads += 1
        self.load_s += seconds

    def note_stage(self, seconds: float) -> None:
        self.stages += 1
        self.stage_s += seconds

    def note_outcome(self, outcome: str) -> None:
        self.outcomes[outcome] = self.outcomes.get(outcome, 0) + 1

    def note_gate(self, elapsed: float, limit: float, *, captured: bool) -> bool:
        """Record the gate's reading and report whether the hook's own condition holds.

        The one reading decides both, so a line can never disagree with what the hook did."""
        self.gate_at_s = elapsed
        self.gate_left_s = limit - elapsed
        if self.drain_only:
            self.pass_state = SKIP_DRAIN_ONLY
        elif not captured:
            self.pass_state = SKIP_NOTHING_CAPTURED
        elif elapsed > limit:
            self.pass_state = SKIP_START_LIMIT
        else:
            self.pass_state = NO_REMOTE  # replaced by the first remote command, if there is one
            return True
        return False

    def enter_pass(self) -> None:
        self.pass_state = ENTERED
        self.pass_started = time.monotonic()

    def leave_pass(self) -> None:
        if self.pass_started is not None:
            self.pass_s = time.monotonic() - self.pass_started
            self.pass_started = None

    def note_timeout(self) -> None:
        self.pass_state = REMOTE_TIMEOUT

    def note_failure(self, stdout: str | None, stderr: str | None) -> None:
        """Only a yes/no is taken from the output. The text itself never leaves this method."""
        locked = _LOCKED in (stdout or "") or _LOCKED in (stderr or "")
        self.pass_state = LOCK_CONTENTION if locked else REMOTE_FAILED

    def note_drain(self, stdout: str) -> None:
        self.pass_state = UNKNOWN
        try:
            payload = json.loads(stdout)
        except (json.JSONDecodeError, TypeError):
            return
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            return
        rows = [row for row in payload.get("results") or [payload.get("result")] if isinstance(row, dict)]
        if not rows:
            return
        attempted = [row for row in rows if row.get("state") != "idle"]
        verified = [row for row in attempted if row.get("state") == "verified"]
        self.rows_attempted = len(attempted)
        self.rows_verified = len(verified)
        if not attempted:
            self.pass_state = IDLE
        elif len(verified) == len(attempted):
            self.pass_state = DELIVERED
        else:
            self.pass_state = PARTIAL if verified else STALLED

    def record(self, seat: str | None, session_id: str | None) -> dict[str, Any]:
        record: dict[str, Any] = {
            "at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "mode": "drain_only" if self.drain_only else "stop",
            "loads": self.loads,
            "load_s": round(self.load_s, 3),
            "stages": self.stages,
            "stage_s": round(self.stage_s, 3),
            "pending": self.pending,
            "older": self.older,
            "older_started": self.older_started,
            "current_polls": self.current_polls,
            "current_skipped": self.current_skipped,
            "outcomes": self.outcomes,
            "gate_at_s": None if self.gate_at_s is None else round(self.gate_at_s, 3),
            "gate_left_s": None if self.gate_left_s is None else round(self.gate_left_s, 3),
            "pass": UNKNOWN if self.pass_state == ENTERED else self.pass_state,
            "pass_s": None if self.pass_s is None else round(self.pass_s, 3),
            "rows_attempted": self.rows_attempted,
            "rows_verified": self.rows_verified,
            "total_s": round(time.monotonic() - self.origin, 3),
            "exit": self.exit,
        }
        for key, value in (("seat", seat), ("session_id", session_id)):
            if isinstance(value, str) and _IDENTIFIER.fullmatch(value):
                record[key] = value
        return record

    def write(self, root: Path, seat: str | None, session_id: str | None) -> None:
        """Rolls over, then appends one line. The hook's `_write_timeline` is the only guard."""
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = root / FILENAME
        if path.exists() and path.stat().st_size >= MAX_BYTES:
            os.replace(path, path.with_name(FILENAME + ".1"))
        line = json.dumps(self.record(seat, session_id), sort_keys=True) + "\n"
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))  # one write: seats share this file
        finally:
            os.close(fd)
