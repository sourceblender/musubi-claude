"""Read one turn's worth of a Claude transcript instead of the whole file.

The Stop hook needs the exchange that just ended: one terminal answer and the
input since the previous terminal answer on the same chain. Until 2026-11-14 it
read the entire transcript to find it, once per candidate and once per poll, up
to ten times a Stop. Measured that day on a seat whose transcript had reached
398 MB: one read took 6.9 s and peaked at 3,351 MB, on every turn.

This module returns the lines of a WINDOW that ends at the end of the file. It
starts at or before the first byte that names the prompt id, and then moves
backward for as long as the caller's `needs` asks it to: either to an offset
the caller names, or by a doubling step when the caller only knows that more is
needed. musubi_claude_turn supplies `needs` and the argument for why the result
stands in for the whole file.

Finding the first byte that names an id is a forward scan in fixed-size chunks,
so memory stays at one chunk however large the transcript is. An id that is not
plain ASCII cannot be searched for as bytes with confidence (JSON may escape
it), so that case reads the whole file, as before.

Pure: no envelope, no harness, no knowledge of what a record means.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO

SCAN_CHUNK_BYTES = 4 * 1024 * 1024
# The previous terminal answer normally sits just before the turn's first record.
# One step back usually finds it; the step doubles when it does not.
FIRST_BACKSTEP_BYTES = 256 * 1024
_LINE_SEARCH_BYTES = 64 * 1024
_SEARCHABLE = re.compile(r"\A[A-Za-z0-9_.:-]+\Z")

# (path, needle) -> (file identity, first offset or None, bytes scanned so far). A
# transcript only grows, so a found offset never moves and a miss only needs the new
# bytes scanned. A different file at the same path starts again.
_first_seen: dict[tuple[str, bytes], tuple[tuple[int, int], int | None, int]] = {}

# (enough, reach): whether the window is enough as it stands, and the lowest byte
# offset the caller knows the window must reach. `reach` equal to the window's own
# start means "nothing further is known to be needed".
Needs = Callable[[list[str], int, BinaryIO], tuple[bool, int]]


class Unsearchable(ValueError):
    """An id whose bytes in the file may differ from its characters."""


def searchable(name: str) -> bool:
    """True when the id's bytes in the file are exactly its characters."""
    return bool(_SEARCHABLE.match(name))


def _scan(handle: BinaryIO, needle: bytes, begin: int, end: int) -> int | None:
    overlap = len(needle) - 1
    position = max(0, begin - overlap)
    handle.seek(position)
    carry = b""
    while position < end:
        chunk = handle.read(min(SCAN_CHUNK_BYTES, end - position))
        if not chunk:
            break
        haystack = carry + chunk
        found = haystack.find(needle)
        if found != -1:
            return position - len(carry) + found
        carry = haystack[-overlap:] if overlap > 0 else b""
        position += len(chunk)
    return None


def first_offset(path: str, handle: BinaryIO, name: str, limit: int) -> int | None:
    """Offset of the first occurrence of `name` that ends within the first `limit` bytes.

    Raises Unsearchable for a name JSON could escape.
    """
    if not searchable(name):
        raise Unsearchable(name)
    needle = name.encode("ascii")
    status = os.fstat(handle.fileno())
    identity = (status.st_dev, status.st_ino)
    key = (path, needle)
    seen_identity, known, scanned = _first_seen.get(key, (identity, None, 0))
    if seen_identity != identity:
        known, scanned = None, 0
    if known is None and scanned < limit:
        known = _scan(handle, needle, scanned, limit)
        scanned = limit
    _first_seen[key] = (identity, known, scanned)
    return known if known is not None and known + len(needle) <= limit else None


def line_start(handle: BinaryIO, offset: int) -> int:
    """The offset of the first byte of the line that contains `offset`."""
    end = offset
    while end > 0:
        begin = max(0, end - _LINE_SEARCH_BYTES)
        handle.seek(begin)
        block = handle.read(end - begin)
        newline = block.rfind(b"\n")
        if newline != -1:
            return begin + newline + 1
        end = begin
    return 0


def _lines_from(handle: BinaryIO, start: int, size: int) -> list[str]:
    handle.seek(start)
    return handle.read(size - start).decode("utf-8").splitlines()


def turn_lines(transcript_path: str, prompt_id: str, needs: Needs) -> list[str]:
    """Lines from the start of the window for `prompt_id` to the end of the file.

    `needs(lines, start, handle)` is asked about each window that does not begin at
    byte 0 (see Needs). Raises OSError exactly where reading the whole file would have.
    """
    path = Path(transcript_path)
    with path.open("rb") as handle:
        size = handle.seek(0, 2)
        try:
            first = first_offset(transcript_path, handle, prompt_id, size)
        except Unsearchable:
            return _lines_from(handle, 0, size)
        if first is None:
            # Nothing names this turn, so no answer can belong to it yet. Any window
            # gives the same "not yet"; return a small one.
            return _lines_from(handle, line_start(handle, max(0, size - FIRST_BACKSTEP_BYTES)), size)
        start = line_start(handle, max(0, line_start(handle, first) - FIRST_BACKSTEP_BYTES))
        step = FIRST_BACKSTEP_BYTES
        while True:
            lines = _lines_from(handle, start, size)
            if start == 0:
                return lines
            try:
                enough, reach = needs(lines, start, handle)
            except Unsearchable:
                return _lines_from(handle, 0, size)
            if enough and reach >= start:
                return lines
            wanted = min(reach, start)
            if not enough:
                step *= 2
                wanted = min(wanted, start - step)
            start = line_start(handle, max(0, wanted))
