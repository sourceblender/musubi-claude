"""One turn of a Claude transcript, read as a window the whole file would agree with.

Joins musubi_claude_window (which bytes) with musubi_claude_exchange (which records
decide the verdict). The window is enough when it holds:

1. The first byte that names the prompt id. A terminal answer belongs to a prompt only
   by walking parentUuid back to a record that carries that promptId, so every answer
   for the prompt, and every record such a walk visits, is inside.
2. The previous terminal answer on the answer's own chain, where the input span starts.
3. Every line that names a deciding message id: each answer for the prompt, and that
   previous answer. The exchange logic treats a message as ALL of its lines. A terminal
   answer counts once, at its first terminal line, so a copy of an old terminal line is
   not a new answer; and an answer's text is every text block that carries its id. With
   every such line inside, both rules, and any later one keyed on a message id, see
   what the whole file would show them. (Yua's two holds on #54; Shiori's rule.)

A message id normally first appears inside its own turn, so rule 3 costs one scan and
moves nothing. An id that also appears far back, as a copy, a parentUuid or quoted
text, only widens the window, which is always safe.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, BinaryIO

from musubi_claude_exchange import window_needs
from musubi_claude_window import first_offset, turn_lines

Parse = Callable[[list[str]], list[dict[str, Any]]]


def load_turn(transcript_path: str, prompt_id: str, parse: Parse) -> list[dict[str, Any]]:
    """The records the exchange `prompt_id` names is resolved from. Raises OSError as a read would."""

    def needs(lines: list[str], start: int, handle: BinaryIO) -> tuple[bool, int]:
        covered, deciding = window_needs(parse(lines), prompt_id)
        reach = start
        for message_id in deciding:
            earliest = first_offset(transcript_path, handle, message_id, start)
            if earliest is not None:
                reach = min(reach, earliest)
        return covered, reach

    return parse(turn_lines(transcript_path, prompt_id, needs))
