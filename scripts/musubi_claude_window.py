"""Read one turn's worth of a Claude transcript instead of the whole file.

The Stop hook needs the exchange that just ended: one terminal answer and the
input since the previous terminal answer on the same chain. Until 2026-11-14 it
read the entire transcript to find it, once per candidate and once per poll, up
to ten times a Stop. Measured that day on a seat whose transcript had reached
398 MB: each Stop held about 3 GB for about twenty seconds, on every turn.

This module returns the lines of a WINDOW that ends at the end of the file and
is guaranteed to hold everything the exchange logic reads for one prompt id:

1. It starts at or before the first byte that names the prompt id. A terminal
   answer belongs to a prompt only by walking parentUuid back to a record that
   carries that promptId, so every answer for it, and every record such a walk
   visits, lies inside the window. "No answer yet" and "several answers" are
   therefore the same verdicts the whole file would give.
2. It then grows backward until `covers(lines)` says the previous same-chain
   terminal answer is inside too, or until it reaches the start of the file.
   That answer is where the exchange's input span begins.

Finding the first byte is a forward scan in fixed-size chunks, so memory stays
at one chunk however large the transcript is. A prompt id that is not plain
ASCII cannot be searched for as bytes with confidence (JSON may escape it), so
that case reads the whole file, as before.

The window is a suffix, and one whole-file rule reaches behind it: a terminal
answer counts once, at its first terminal line. A copy of an earlier terminal
line inside the window is not a new answer. `named_before` lets the caller ask
whether a line before the window names an id and satisfies a predicate, again
by a chunked scan, so the caller can keep that rule for the ids that matter.

Pure: no envelope, no harness, no knowledge of what a record means. The caller
supplies the predicates.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import BinaryIO

SCAN_CHUNK_BYTES = 4 * 1024 * 1024
# The previous terminal answer normally sits just before the turn's first record.
# One step back usually finds it; the step doubles when it does not.
FIRST_BACKSTEP_BYTES = 256 * 1024
_LINE_SEARCH_BYTES = 64 * 1024
_SEARCHABLE = re.compile(r"\A[A-Za-z0-9_.:-]+\Z")

# (path, needle) -> (first offset or None, bytes scanned so far). A transcript only
# grows, so a found offset never moves and a miss only needs the new bytes scanned.
_first_seen: dict[tuple[str, bytes], tuple[int | None, int]] = {}


# (path, needle) -> (offset of the first accepted line or None, bytes examined so far).
_named_seen: dict[tuple[str, bytes], tuple[int | None, int]] = {}


class Unsearchable(ValueError):
    """An id whose bytes in the file may differ from its characters."""


def searchable(prompt_id: str) -> bool:
    """True when the id's bytes in the file are exactly its characters."""
    return bool(_SEARCHABLE.match(prompt_id))


def _scan(handle: BinaryIO, needle: bytes, begin: int, size: int) -> int | None:
    overlap = len(needle) - 1
    position = max(0, begin - overlap)
    handle.seek(position)
    carry = b""
    while position < size:
        chunk = handle.read(min(SCAN_CHUNK_BYTES, size - position))
        if not chunk:
            break
        haystack = carry + chunk
        found = haystack.find(needle)
        if found != -1:
            return position - len(carry) + found
        carry = haystack[-overlap:] if overlap > 0 else b""
        position += len(chunk)
    return None


def first_offset(path: str, handle: BinaryIO, needle: bytes, size: int) -> int | None:
    """Offset of the first occurrence of `needle` in the first `size` bytes."""
    key = (path, needle)
    known, scanned = _first_seen.get(key, (None, 0))
    if known is not None and known + len(needle) <= size:
        return known
    if scanned > size:  # the file was replaced by a shorter one; start again
        scanned = 0
    found = _scan(handle, needle, scanned, size)
    _first_seen[key] = (found, size)
    return found


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


def _occurrences(handle: BinaryIO, needle: bytes, begin: int, end: int) -> Iterator[int]:
    """Every offset in [begin, end) where `needle` starts, in order, in bounded chunks."""
    overlap = len(needle) - 1
    position = max(0, begin - overlap)
    carry = b""
    floor = begin
    while position < end:
        handle.seek(position)
        chunk = handle.read(min(SCAN_CHUNK_BYTES, end - position))
        if not chunk:
            return
        haystack = carry + chunk
        base = position - len(carry)
        at = haystack.find(needle)
        while at != -1:
            if base + at >= floor:
                yield base + at
                floor = base + at + 1
            at = haystack.find(needle, at + 1)
        carry = haystack[-overlap:] if overlap > 0 else b""
        position += len(chunk)


def _line_at(handle: BinaryIO, offset: int, size: int) -> tuple[int, str]:
    """(end offset, text) of the line that contains `offset`."""
    start = line_start(handle, offset)
    end = start
    handle.seek(start)
    parts: list[bytes] = []
    while end < size:
        block = handle.read(min(_LINE_SEARCH_BYTES, size - end))
        if not block:
            break
        newline = block.find(b"\n")
        if newline != -1:
            parts.append(block[:newline])
            end += newline + 1
            break
        parts.append(block)
        end += len(block)
    return end, b"".join(parts).decode("utf-8")


def named_before(path: str, handle: BinaryIO, name: str, limit: int, accept: Callable[[str], bool]) -> bool:
    """Whether some line that starts before `limit` contains `name` and satisfies `accept`.

    `accept` receives the whole line. Raises Unsearchable for a name JSON could escape.
    """
    if not searchable(name):
        raise Unsearchable(name)
    needle = name.encode("ascii")
    key = (path, needle)
    found, examined = _named_seen.get(key, (None, 0))
    if found is not None:
        return found < limit
    if examined >= limit:
        return False
    skip_to = examined
    for offset in _occurrences(handle, needle, examined, limit):
        if offset < skip_to:
            continue  # same line as an occurrence already judged
        end, text = _line_at(handle, offset, limit)
        if accept(text):
            _named_seen[key] = (line_start(handle, offset), limit)
            return True
        skip_to = end
    _named_seen[key] = (None, limit)
    return False


def _lines_from(handle: BinaryIO, start: int, size: int) -> list[str]:
    handle.seek(start)
    return handle.read(size - start).decode("utf-8").splitlines()


def turn_lines(
    transcript_path: str,
    prompt_id: str,
    covers: Callable[[list[str], int, BinaryIO], bool],
) -> tuple[list[str], int]:
    """(lines, start): the smallest window (see module docstring) for `prompt_id`, and
    the byte offset it begins at. `start` is 0 when the whole file was read.

    `covers(lines, start, handle)` says whether the window that begins at byte `start`
    is enough; it may call `named_before` with `handle` and `start` as the limit. It is
    not asked about a window that begins at 0, which has nothing before it.
    Raises OSError exactly where reading the whole file would have.
    """
    path = Path(transcript_path)
    with path.open("rb") as handle:
        size = handle.seek(0, 2)
        if not searchable(prompt_id):
            return _lines_from(handle, 0, size), 0
        first = first_offset(transcript_path, handle, prompt_id.encode("ascii"), size)
        if first is None:
            # Nothing names this turn, so no answer can belong to it yet. Any window
            # gives the same "not yet"; return a small one.
            start = line_start(handle, max(0, size - FIRST_BACKSTEP_BYTES))
            return _lines_from(handle, start, size), start
        start = line_start(handle, first)
        step = FIRST_BACKSTEP_BYTES
        while True:
            start = line_start(handle, max(0, start - step))
            lines = _lines_from(handle, start, size)
            try:
                enough = start == 0 or covers(lines, start, handle)
            except Unsearchable:
                return _lines_from(handle, 0, size), 0
            if enough:
                return lines, start
            step *= 2
