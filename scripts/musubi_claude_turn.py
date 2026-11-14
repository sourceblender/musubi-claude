"""One turn of a Claude transcript, read as a window the whole file would agree with.

Joins musubi_claude_window (which bytes) with musubi_claude_exchange (which records
decide the verdict). The result is the window's records plus `preceded`: the ids of
deciding answers that were already terminal before the window, so that a copy of one
inside the window is not counted as a new answer. Pass both to resolve_anchor and
exchange_for.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, BinaryIO

from musubi_claude_exchange import is_terminal_answer, window_needs
from musubi_claude_window import named_before, turn_lines

Parse = Callable[[list[str]], list[dict[str, Any]]]


def load_turn(transcript_path: str, prompt_id: str, parse: Parse) -> tuple[list[dict[str, Any]], frozenset[str]]:
    """(records, preceded) for the exchange `prompt_id` names. Raises OSError as a read would."""
    settled: dict[int, frozenset[str]] = {}

    def covers(lines: list[str], start: int, handle: BinaryIO) -> bool:
        records = parse(lines)
        preceded: set[str] = set()
        checked: set[str] = set()
        while True:
            # Dropping a copy can change which answers decide the verdict, so ask again
            # until every deciding id has been looked for before the window.
            covered, deciding = window_needs(records, prompt_id, frozenset(preceded))
            fresh = [message_id for message_id in deciding if message_id not in checked]
            if not fresh:
                settled[start] = frozenset(preceded)
                return covered
            for message_id in fresh:
                checked.add(message_id)

                def terminal_line(text: str, wanted: str = message_id) -> bool:
                    return any(is_terminal_answer(record, wanted) for record in parse(text.splitlines()))

                if named_before(transcript_path, handle, message_id, start, terminal_line):
                    preceded.add(message_id)

    lines, start = turn_lines(transcript_path, prompt_id, covers)
    return parse(lines), settled.get(start, frozenset())
