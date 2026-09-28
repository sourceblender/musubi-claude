"""Pending Stop candidates: an answer whose transcript record was not on disk yet.

Measured 2026-09-28 with a read-only probe on a live seat: at Stop entry the terminal
answer for the Stop's prompt_id was absent from the transcript 2 of 2 times, and both
resolved afterwards to exactly one terminal answer whose record timestamp equalled the
Stop time to the second. Claude writes the final a beat after Stop fires. So a Stop
that cannot anchor yet is not a failure; it is a candidate that a later hook resolves.

One JSON file per candidate under <seat>/<zone>/pending/, written atomically with
owner-only permissions. It holds the answer text, which is conversation content at the
same trust level as shadow.db beside it, and it is deleted as soon as it resolves or
expires. Nothing here talks to the harness.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from pathlib import Path

# A candidate that has not resolved after this many drain attempts, or this long, is
# declined visibly. The race measured is sub-second; these bounds are for a transcript
# that was rotated, truncated or never completed.
MAX_ATTEMPTS = 20
MAX_AGE_SECONDS = 24 * 3600


@dataclass
class Candidate:
    session_id: str
    prompt_id: str
    transcript_path: str
    answer_text: str
    is_sidechain_hint: bool
    created_at: float
    attempts: int = 0

    @property
    def key(self) -> str:
        return candidate_key(self.session_id, self.prompt_id)


def candidate_key(session_id: str, prompt_id: str) -> str:
    return hashlib.sha256(f"{session_id}\0{prompt_id}".encode()).hexdigest()[:32]


def pending_dir(seat_root: Path) -> Path:
    return seat_root / "pending"


def save(seat_root: Path, candidate: Candidate) -> Path:
    """Write or overwrite the candidate for (session, prompt). Atomic, 0600."""
    directory = pending_dir(seat_root)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = directory / f"{candidate.key}.json"
    tmp = directory / f".{candidate.key}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(asdict(candidate), handle, ensure_ascii=False, sort_keys=True)
        os.replace(tmp, target)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    return target


def load_all(seat_root: Path) -> Iterator[tuple[Path, Candidate | None]]:
    """Every candidate file, oldest first. A file that cannot be read yields None."""
    directory = pending_dir(seat_root)
    if not directory.is_dir():
        return
    files = sorted(directory.glob("*.json"), key=lambda p: p.stat().st_mtime)
    for path in files:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            yield path, Candidate(**raw)
        except (OSError, ValueError, TypeError):
            yield path, None


def expired(candidate: Candidate, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    return candidate.attempts >= MAX_ATTEMPTS or now - candidate.created_at > MAX_AGE_SECONDS


def remove(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def load(seat_root: Path, key: str) -> Candidate | None:
    """The saved candidate for this key, or None if absent or unreadable."""
    path = pending_dir(seat_root) / f"{key}.json"
    try:
        return Candidate(**json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        return None
