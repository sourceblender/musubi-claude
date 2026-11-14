"""The Stop hook reads one turn's window of the transcript, not the whole file.

Each case builds a transcript, resolves the exchange from the whole file and from the
window, and requires the same answer. The size cases prove the window is small and that
finding it does not hold the file in memory.
"""

from __future__ import annotations

import json
import sys
import tracemalloc
from pathlib import Path
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import musubi_claude_window as window  # noqa: E402
from musubi_claude_exchange import (  # noqa: E402
    ExchangeError,
    exchange_for,
    iter_dicts,
    resolve_anchor,
)
from musubi_claude_turn import load_turn  # noqa: E402

SESSION = "11111111-1111-4111-8111-111111111111"


class Tx:
    """A transcript in the record shapes the exchange tests use."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.records: list[dict[str, Any]] = []
        self._n = 0
        self._last: str | None = None

    def _id(self) -> str:
        self._n += 1
        return f"u{self._n:06d}"

    def user(self, prompt: str, text: str, *, sidechain: bool = False) -> str:
        uuid = self._id()
        self.records.append(
            {
                "type": "user",
                "uuid": uuid,
                "parentUuid": self._last,
                "promptId": prompt,
                "promptSource": "typed",
                "sessionId": SESSION,
                "isSidechain": sidechain,
                "message": {"role": "user", "content": text},
            }
        )
        self._last = uuid
        return uuid

    def answer(self, message_id: str, text: str, *, sidechain: bool = False, stop: str = "end_turn") -> None:
        uuid = self._id()
        self.records.append(
            {
                "type": "assistant",
                "uuid": uuid,
                "parentUuid": self._last,
                "sessionId": SESSION,
                "isSidechain": sidechain,
                "message": {"id": message_id, "stop_reason": stop, "content": [{"type": "text", "text": text}]},
            }
        )
        self._last = uuid

    def turn(self, n: int, pad: int = 0) -> None:
        self.user(f"prompt-{n:05d}", f"question {n} " + "x" * pad)
        self.answer(f"msg_{n:05d}", f"answer {n}")

    def write(self) -> Path:
        self.path.write_text("\n".join(json.dumps(r) for r in self.records) + "\n", encoding="utf-8")
        return self.path


def _parse(lines: list[str]) -> list[dict[str, Any]]:
    return iter_dicts(json.loads(line) for line in lines if line.strip())


def _whole(path: Path) -> list[dict[str, Any]]:
    return _parse(path.read_text(encoding="utf-8").splitlines())


def _windowed(path: Path, prompt: str) -> list[dict[str, Any]]:
    return load_turn(str(path), prompt, _parse)


def _resolve(records: list[dict[str, Any]], prompt: str, text: str) -> Any:
    try:
        return exchange_for(records, resolve_anchor(records, prompt, text))
    except ExchangeError as exc:
        return f"declined:{exc}"


@pytest.fixture(autouse=True)
def _fresh_cache() -> None:
    window._first_seen.clear()


def test_window_gives_the_same_exchange_as_the_whole_file(tmp_path: Path) -> None:
    tx = Tx(tmp_path / "t.jsonl")
    for n in range(400):
        tx.turn(n, pad=2000)
    path = tx.write()
    for n in (0, 1, 199, 399):
        prompt, text = f"prompt-{n:05d}", f"answer {n}"
        assert _resolve(_windowed(path, prompt), prompt, text) == _resolve(_whole(path), prompt, text)


def test_window_is_the_turn_not_the_file(tmp_path: Path) -> None:
    tx = Tx(tmp_path / "t.jsonl")
    for n in range(3000):
        tx.turn(n, pad=4000)
    path = tx.write()
    records = _windowed(path, "prompt-02999")
    # 6000 records in the file; the last turn and the answer before it are what is needed.
    assert len(records) < 200
    assert records[-1]["message"]["id"] == "msg_02999"


def test_finding_the_window_does_not_hold_the_file_in_memory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(window, "SCAN_CHUNK_BYTES", 256 * 1024)
    tx = Tx(tmp_path / "t.jsonl")
    for n in range(3000):
        tx.turn(n, pad=8000)
    path = tx.write()
    size = path.stat().st_size
    assert size > 20 * 1024 * 1024
    tracemalloc.start()
    load_turn(str(path), "prompt-02999", _parse)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert peak < size / 8, f"peak {peak} for a {size}-byte file"


def test_span_start_far_behind_the_first_step_is_still_found(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(window, "FIRST_BACKSTEP_BYTES", 512)
    tx = Tx(tmp_path / "t.jsonl")
    tx.turn(1)
    tx.user("prompt-00002", "first part " + "y" * 50_000)
    tx.user("prompt-00002", "second part " + "z" * 50_000)
    tx.answer("msg_00002", "answer 2")
    path = tx.write()
    got = _resolve(_windowed(path, "prompt-00002"), "prompt-00002", "answer 2")
    assert got == _resolve(_whole(path), "prompt-00002", "answer 2")
    assert not isinstance(got, str)


def test_other_chain_answer_does_not_stand_in_for_the_span_start(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(window, "FIRST_BACKSTEP_BYTES", 2000)
    tx = Tx(tmp_path / "t.jsonl")
    tx.turn(1)
    tx.user("prompt-main", "the real question " + "q" * 30_000)
    # A subagent's terminal answer sits between the main prompt and the main answer.
    tx.user("prompt-side", "side question", sidechain=True)
    tx.answer("msg_side", "side answer", sidechain=True)
    tx.answer("msg_main", "main answer")
    path = tx.write()
    assert _resolve(_windowed(path, "prompt-main"), "prompt-main", "main answer") == _resolve(_whole(path), "prompt-main", "main answer")


def test_no_answer_yet_is_not_yet_in_the_window_too(tmp_path: Path) -> None:
    tx = Tx(tmp_path / "t.jsonl")
    for n in range(50):
        tx.turn(n, pad=20_000)
    tx.user("prompt-open", "asked, not answered")
    path = tx.write()
    assert _resolve(_windowed(path, "prompt-open"), "prompt-open", "anything") == "declined:answer_not_in_transcript"
    assert _resolve(_whole(path), "prompt-open", "anything") == "declined:answer_not_in_transcript"


def test_a_prompt_the_file_never_names_reads_a_small_tail(tmp_path: Path) -> None:
    tx = Tx(tmp_path / "t.jsonl")
    for n in range(500):
        tx.turn(n, pad=4000)
    path = tx.write()
    records = _windowed(path, "prompt-absent")
    assert len(records) < len(tx.records) / 4
    assert _resolve(records, "prompt-absent", "x") == "declined:answer_not_in_transcript"


def test_two_answers_for_one_prompt_are_still_ambiguous(tmp_path: Path) -> None:
    tx = Tx(tmp_path / "t.jsonl")
    for n in range(50):
        tx.turn(n, pad=20_000)
    tx.user("prompt-twice", "one question")
    tx.answer("msg_first", "first answer")
    tx.answer("msg_second", "second answer")
    path = tx.write()
    assert _resolve(_windowed(path, "prompt-twice"), "prompt-twice", "second answer") == "declined:answer_ambiguous"
    assert _resolve(_whole(path), "prompt-twice", "second answer") == "declined:answer_ambiguous"


def test_an_id_that_json_could_escape_reads_the_whole_file(tmp_path: Path) -> None:
    tx = Tx(tmp_path / "t.jsonl")
    for n in range(20):
        tx.turn(n)
    tx.user('prompt "quoted"', "odd id")
    tx.answer("msg_odd", "odd answer")
    path = tx.write()
    assert not window.searchable('prompt "quoted"')
    assert len(_windowed(path, 'prompt "quoted"')) == len(tx.records)


def test_the_id_quoted_in_earlier_text_only_widens_the_window(tmp_path: Path) -> None:
    tx = Tx(tmp_path / "t.jsonl")
    tx.turn(1)
    tx.user("prompt-00002", "someone pasted prompt-00009 into a message")
    tx.answer("msg_00002", "answer 2")
    for n in range(3, 9):
        tx.turn(n)
    tx.turn(9)
    path = tx.write()
    assert _resolve(_windowed(path, "prompt-00009"), "prompt-00009", "answer 9") == _resolve(_whole(path), "prompt-00009", "answer 9")


def test_a_file_that_grows_between_polls_is_scanned_only_where_it_grew(tmp_path: Path) -> None:
    tx = Tx(tmp_path / "t.jsonl")
    for n in range(100):
        tx.turn(n, pad=2000)
    path = tx.write()
    assert _resolve(_windowed(path, "prompt-late"), "prompt-late", "late") == "declined:answer_not_in_transcript"
    assert window._first_seen[(str(path), b"prompt-late")][1:] == (None, path.stat().st_size)
    tx.user("prompt-late", "arrived")
    tx.answer("msg_late", "late")
    tx.write()
    got = _resolve(_windowed(path, "prompt-late"), "prompt-late", "late")
    assert not isinstance(got, str)
    assert got == _resolve(_whole(path), "prompt-late", "late")


def test_a_missing_transcript_raises_oserror(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        load_turn(str(tmp_path / "gone.jsonl"), "prompt-1", _parse)


# --- The exchange logic reads a message across ALL its lines (Yua's two holds on #54) ---


def _ids(result: Any) -> Any:
    """What must be identical: the answer, and the ids of the inputs it captured."""
    if isinstance(result, str):
        return result
    return (result.answer_id, tuple(item.record_id for item in result.inputs), result.trigger)


def _far_back(tx: Tx) -> None:
    for n in range(50):
        tx.turn(n, pad=400)


def test_a_copy_of_an_old_terminal_line_is_not_the_previous_answer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Old terminal A, then enough to push A out of the first window, a normal previous
    turn B, the target's input, an exact copy of A's line, then the target's answer. The
    whole file counts A once, at its first line, so the span starts after B."""
    monkeypatch.setattr(window, "FIRST_BACKSTEP_BYTES", 512)
    tx = Tx(tmp_path / "t.jsonl")
    tx.user("ask-old", "an old question")
    tx.answer("msg_A", "an old answer")
    old_terminal = dict(tx.records[-1])
    _far_back(tx)
    tx.user("ask-B", "the previous question")
    tx.answer("msg_B", "the previous answer")
    captured = tx.user("ask-target", "the question that must be captured")
    tx.records.append(old_terminal)  # the exact duplicate, mid-turn
    tx.answer("msg_target", "the target answer")
    path = tx.write()
    whole = _resolve(_whole(path), "ask-target", "the target answer")
    windowed = _resolve(_windowed(path, "ask-target"), "ask-target", "the target answer")
    assert not isinstance(whole, str)
    assert _ids(windowed) == _ids(whole)
    assert captured in _ids(whole)[1]


def test_a_reparented_copy_of_an_old_answer_is_not_a_second_answer_for_the_prompt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A copy of an old terminal line whose parent now sits in the target's turn would
    resolve to the target's prompt in a window that ended there. The file never counts it."""
    monkeypatch.setattr(window, "FIRST_BACKSTEP_BYTES", 512)
    tx = Tx(tmp_path / "t.jsonl")
    tx.user("ask-old", "an old question")
    tx.answer("msg_A", "an old answer")
    old_terminal = dict(tx.records[-1])
    _far_back(tx)
    asked = tx.user("ask-target", "the question")
    tx.records.append({**old_terminal, "uuid": "copy-of-A", "parentUuid": asked})
    tx.answer("msg_target", "the target answer")
    path = tx.write()
    whole = _resolve(_whole(path), "ask-target", "the target answer")
    windowed = _resolve(_windowed(path, "ask-target"), "ask-target", "the target answer")
    assert not isinstance(whole, str), whole
    assert _ids(windowed) == _ids(whole)


def test_an_answer_whose_id_was_terminal_long_ago_declines_as_the_whole_file_does(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(window, "FIRST_BACKSTEP_BYTES", 512)
    tx = Tx(tmp_path / "t.jsonl")
    tx.user("ask-old", "an old question")
    tx.answer("msg_reused", "an old answer")
    _far_back(tx)
    tx.user("ask-target", "the question")
    tx.answer("msg_reused", "an old answer")
    path = tx.write()
    whole = _resolve(_whole(path), "ask-target", "an old answer")
    assert isinstance(whole, str)
    assert _resolve(_windowed(path, "ask-target"), "ask-target", "an old answer") == whole


def test_earlier_text_of_the_answer_is_part_of_the_binding_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Yua's second hold. An old, non-terminal line carries text under the id the target's
    answer later uses. An answer's text is every text block with its id, so the whole file
    sees old text plus new and declines the mismatch. A window without the old line would
    see only the new text and accept a binding the file refuses."""
    monkeypatch.setattr(window, "FIRST_BACKSTEP_BYTES", 512)
    tx = Tx(tmp_path / "t.jsonl")
    tx.user("ask-old", "an old question")
    tx.answer("msg_M", "text from long ago", stop="tool_use")
    tx.answer("msg_old_end", "the old turn ends")
    _far_back(tx)
    tx.user("ask-B", "the previous question")
    tx.answer("msg_B", "the previous answer")
    tx.user("ask-target", "the question")
    tx.answer("msg_M", "what Stop says")
    path = tx.write()
    whole = _resolve(_whole(path), "ask-target", "what Stop says")
    assert whole == "declined:answer_text_mismatch"
    assert _resolve(_windowed(path, "ask-target"), "ask-target", "what Stop says") == whole


def test_an_earlier_line_of_the_previous_answer_changes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The control: a message spans lines and only its terminal line counts as the answer."""
    monkeypatch.setattr(window, "FIRST_BACKSTEP_BYTES", 300)
    tx = Tx(tmp_path / "t.jsonl")
    tx.turn(1)
    tx.user("ask-B", "the previous question")
    tx.answer("msg_B", "thinking " + "t" * 3000, stop="tool_use")
    tx.answer("msg_B", "the previous answer")
    captured = tx.user("ask-target", "the question")
    tx.answer("msg_target", "the target answer")
    path = tx.write()
    whole = _resolve(_whole(path), "ask-target", "the target answer")
    windowed = _resolve(_windowed(path, "ask-target"), "ask-target", "the target answer")
    assert _ids(windowed) == _ids(whole)
    assert _ids(whole)[1] == (captured,)


def test_a_message_id_quoted_far_back_only_widens_the_window(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(window, "FIRST_BACKSTEP_BYTES", 512)
    tx = Tx(tmp_path / "t.jsonl")
    tx.user("ask-old", "someone pasted msg_target into a message")
    tx.answer("msg_old", "noted")
    _far_back(tx)
    tx.user("ask-target", "the question")
    tx.answer("msg_target", "the target answer")
    path = tx.write()
    records = _windowed(path, "ask-target")
    assert len(records) == len(tx.records), "the quoted id is the first byte that names it"
    assert _ids(_resolve(records, "ask-target", "the target answer")) == _ids(_resolve(_whole(path), "ask-target", "the target answer"))


def test_a_file_replaced_at_the_same_path_is_scanned_afresh(tmp_path: Path) -> None:
    first = Tx(tmp_path / "t.jsonl")
    for n in range(30):
        first.turn(n, pad=2000)
    path = first.write()
    assert not isinstance(_resolve(_windowed(path, "prompt-00029"), "prompt-00029", "answer 29"), str)
    path.unlink()
    second = Tx(tmp_path / "t.jsonl")
    second.user("prompt-00029", "a different session reusing the id")
    second.answer("msg_other", "a different answer")
    second.write()
    got = _resolve(_windowed(path, "prompt-00029"), "prompt-00029", "a different answer")
    assert _ids(got) == _ids(_resolve(_whole(path), "prompt-00029", "a different answer"))
