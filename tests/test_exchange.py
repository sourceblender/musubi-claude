"""Exchange identity walker: the contract's conformance cases on Claude transcript shapes.

Record shapes are copied from fields measured on real Claude transcripts
(2026-09-28): promptSource typed|queued|system|absent, origin.kind
human|task-notification|peer, turnOrigin scheduled, isMeta, isCompactSummary,
command-name/local-command-stdout pairs linked by parentUuid, and assistant
messages that span several lines under one message.id.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from scripts import musubi_claude_exchange as X

SESSION = "11111111-2222-3333-4444-555555555555"


class T:
    """A tiny transcript builder that chains parentUuid in append order."""

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []
        self._n = 0
        self.last: str | None = None

    def _uuid(self) -> str:
        self._n += 1
        return f"u{self._n:04d}"

    def user(
        self, text: str, prompt: str, *, source: str | None = "typed", origin: str | None = "human", sidechain: bool = False, **extra: Any
    ) -> str:
        uid = self._uuid()
        record: dict[str, Any] = {
            "type": "user",
            "uuid": uid,
            "parentUuid": self.last,
            "sessionId": SESSION,
            "promptId": prompt,
            "isSidechain": sidechain,
            "message": {"role": "user", "content": text},
        }
        if source is not None:
            record["promptSource"] = source
        if origin is not None:
            record["origin"] = {"kind": origin}
        record.update(extra)
        self.records.append(record)
        self.last = uid
        return uid

    def tool_result(self, prompt: str) -> str:
        uid = self._uuid()
        self.records.append(
            {
                "type": "user",
                "uuid": uid,
                "parentUuid": self.last,
                "sessionId": SESSION,
                "promptId": prompt,
                "message": {"role": "user", "content": [{"type": "tool_result", "content": "x"}]},
            }
        )
        self.last = uid
        return uid

    def assistant(self, msg: str, text: str | None, stop: str | None, *, sidechain: bool = False) -> str:
        uid = self._uuid()
        content = [{"type": "text", "text": text}] if text is not None else [{"type": "tool_use"}]
        self.records.append(
            {
                "type": "assistant",
                "uuid": uid,
                "parentUuid": self.last,
                "sessionId": SESSION,
                "isSidechain": sidechain,
                "message": {"id": msg, "role": "assistant", "content": content, "stop_reason": stop},
            }
        )
        self.last = uid
        return uid


def exchange_at(t: T, answer_id: str) -> X.Exchange:
    return X.exchange_for(t.records, answer_id)


def test_one_input_one_answer() -> None:
    t = T()
    u = t.user("hello", "p1")
    t.assistant("m1", "hi", "end_turn")
    e = exchange_at(t, "m1")
    assert [i.record_id for i in e.inputs] == [u]
    assert e.trigger is None
    assert X.exchange_id(e.session_id, e.answer_id) == f"exchange.v1:claude-code:{SESSION}:m1"


def test_typed_then_queued_both_in_order() -> None:
    t = T()
    a = t.user("first", "p1")
    b = t.user("second", "p1", source="queued")
    t.assistant("m1", "both", "end_turn")
    assert [i.text for i in exchange_at(t, "m1").inputs] == ["first", "second"]
    assert [i.record_id for i in exchange_at(t, "m1").inputs] == [a, b]


def test_span_starts_after_previous_terminal_answer() -> None:
    t = T()
    t.user("one", "p1")
    t.assistant("m1", "a1", "end_turn")
    two = t.user("two", "p2")
    t.assistant("m2", "a2", "end_turn")
    assert [i.record_id for i in exchange_at(t, "m2").inputs] == [two]


def test_compaction_summary_is_not_voice() -> None:
    t = T()
    u = t.user("question", "p1")
    t.user("summary of everything", "p1", source=None, origin=None, isCompactSummary=True, isVisibleInTranscriptOnly=True)
    t.assistant("m1", "answer", "end_turn")
    assert [i.record_id for i in exchange_at(t, "m1").inputs] == [u]


def test_meta_hook_context_is_not_voice() -> None:
    t = T()
    u = t.user("question", "p1")
    t.user("<system-reminder>ctx</system-reminder>", "p1", source=None, origin=None, isMeta=True)
    t.assistant("m1", "answer", "end_turn")
    assert [i.record_id for i in exchange_at(t, "m1").inputs] == [u]


def test_no_final_turn_folds_forward() -> None:
    t = T()
    early = t.user("burst one", "p1")
    late = t.user("burst two", "p2")
    t.assistant("m1", "answered both", "end_turn")
    assert [i.record_id for i in exchange_at(t, "m1").inputs] == [early, late]


def test_interrupted_tool_use_folds_forward() -> None:
    t = T()
    first = t.user("do the thing", "p1")
    t.assistant("m0", None, "tool_use")
    second = t.user("actually stop, do this", "p2")
    t.assistant("m1", "ok", "end_turn")
    assert [i.record_id for i in exchange_at(t, "m1").inputs] == [first, second]


@pytest.mark.parametrize("stop", ["end_turn", "stop_sequence", "refusal"])
def test_every_terminal_stop_reason_anchors(stop: str) -> None:
    t = T()
    t.user("q", "p1")
    t.assistant("m1", "a", stop)
    assert exchange_at(t, "m1").answer_id == "m1"


@pytest.mark.parametrize("literal", ["<local-command-stdout>hi</local-command-stdout>", "<command-name>/clear</command-name>", "/clear"])
def test_literal_marker_in_typed_text_is_voice(literal: str) -> None:
    t = T()
    u = t.user(literal, "p1")
    t.assistant("m1", "a", "end_turn")
    e = exchange_at(t, "m1")
    assert [i.record_id for i in e.inputs] == [u]
    assert e.trigger is None


def test_paired_slash_command_is_trigger_and_stdout_excluded() -> None:
    t = T()
    cmd = t.user("<command-name>/model</command-name>", "p1", source=None, origin=None)
    t.user("<local-command-stdout>set</local-command-stdout>", "p1", source=None, origin=None)
    t.assistant("m1", "noted", "end_turn")
    e = exchange_at(t, "m1")
    assert e.inputs == ()
    assert e.trigger is not None and (e.trigger.trigger_class, e.trigger.record_id) == ("slash-command", cmd)


def test_unpaired_skill_command_is_trigger() -> None:
    t = T()
    cmd = t.user("<command-name>/morning</command-name>", "p1", source=None, origin=None)
    t.assistant("m1", "morning report", "end_turn")
    e = exchange_at(t, "m1")
    assert e.trigger is not None and e.trigger.record_id == cmd


def test_unpaired_stdout_declines() -> None:
    t = T()
    t.user("<local-command-stdout>orphan</local-command-stdout>", "p1", source=None, origin=None)
    t.assistant("m1", "a", "end_turn")
    with pytest.raises(X.ExchangeError, match="stdout_unpaired"):
        exchange_at(t, "m1")


@pytest.mark.parametrize(
    ("fields", "cls"),
    [
        ({"source": "system", "origin": "task-notification"}, "task-notification"),
        ({"source": "system", "origin": "peer"}, "peer-message"),
        ({"source": "system", "origin": None, "turnOrigin": "scheduled", "isMeta": True}, "scheduled"),
    ],
)
def test_machine_triggers(fields: dict[str, Any], cls: str) -> None:
    t = T()
    rid = t.user("machine text", "p1", **fields)
    t.assistant("m1", "report", "end_turn")
    e = exchange_at(t, "m1")
    assert e.inputs == ()
    assert e.trigger is not None and (e.trigger.trigger_class, e.trigger.record_id, e.trigger.text) == (cls, rid, "machine text")


def test_trigger_is_dropped_when_voice_exists() -> None:
    t = T()
    t.user("agent done", "p1", source="system", origin="task-notification")
    u = t.user("so what did it find", "p2")
    t.assistant("m1", "it found x", "end_turn")
    e = exchange_at(t, "m1")
    assert [i.record_id for i in e.inputs] == [u]
    assert e.trigger is None


def test_two_triggers_without_voice_decline() -> None:
    t = T()
    t.user("agent one done", "p1", source="system", origin="task-notification")
    t.user("agent two done", "p2", source="system", origin="task-notification")
    t.assistant("m1", "both done", "end_turn")
    with pytest.raises(X.ExchangeError, match="trigger_ambiguous"):
        exchange_at(t, "m1")


def test_no_input_no_trigger_declines() -> None:
    t = T()
    t.user("q", "p1")
    t.assistant("m1", "a", "end_turn")
    t.tool_result("p1")
    t.assistant("m2", "continuation", "end_turn")
    with pytest.raises(X.ExchangeError, match="no_eligible_input"):
        exchange_at(t, "m2")


def test_unlabelled_plain_record_declines() -> None:
    t = T()
    t.user("who said this", "p1", source=None, origin=None)
    t.assistant("m1", "a", "end_turn")
    with pytest.raises(X.ExchangeError, match="input_unclassified"):
        exchange_at(t, "m1")


def test_sidechain_records_stay_out_of_main_span() -> None:
    t = T()
    u = t.user("main question", "p1")
    t.user("subagent prompt", "p1", sidechain=True)
    t.assistant("s1", "subagent answer", "end_turn", sidechain=True)
    t.assistant("m1", "main answer", "end_turn")
    assert [i.record_id for i in exchange_at(t, "m1").inputs] == [u]


def test_multiline_message_counts_once_and_joins_text() -> None:
    t = T()
    t.user("q", "p1")
    t.assistant("m1", "part one", None)
    t.assistant("m1", "part two", "end_turn")
    assert [mid for _, mid, _ in X._terminal_answers(t.records)] == ["m1"]
    assert X.resolve_anchor(t.records, "p1", "part one\n\npart two") == "m1"


def test_resolve_anchor_follows_tool_results_to_prompt() -> None:
    t = T()
    t.user("q", "p1")
    t.assistant("m0", None, "tool_use")
    t.tool_result("p1")
    t.assistant("m1", "done", "end_turn")
    assert X.resolve_anchor(t.records, "p1", "done") == "m1"


def test_resolve_anchor_refuses_text_mismatch() -> None:
    t = T()
    t.user("q", "p1")
    t.assistant("m1", "what the transcript says", "end_turn")
    with pytest.raises(X.ExchangeError, match="answer_text_mismatch"):
        X.resolve_anchor(t.records, "p1", "what the Stop event says")


def test_resolve_anchor_declines_when_answer_not_written_yet() -> None:
    t = T()
    t.user("q", "p1")
    with pytest.raises(X.ExchangeError, match="answer_not_in_transcript"):
        X.resolve_anchor(t.records, "p1", "a")


def test_replay_is_deterministic() -> None:
    t = T()
    t.user("q", "p1", source="queued")
    t.user("r", "p1")
    t.assistant("m1", "a", "end_turn")
    assert exchange_at(t, "m1") == exchange_at(t, "m1")


def test_exchange_id_satisfies_harness_event_re() -> None:
    # Copied from musubi_harness.core.EVENT_RE at 1.6.0.
    event_re = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,191}$")
    real = X.exchange_id("6fc02f76-0b0c-4f5e-9f1f-6b1e2c1d9a3b", "msg_01ABCdefGHIjklMNOpqrSTUv")
    assert event_re.fullmatch(real)


@pytest.mark.parametrize("source", ["sdk", "suggestion_accepted"])
def test_non_terminal_input_channels_are_voice(source: str) -> None:
    t = T()
    u = t.user("from an IDE or SDK caller", "p1", source=source)
    t.assistant("m1", "a", "end_turn")
    assert [i.record_id for i in exchange_at(t, "m1").inputs] == [u]


def test_skill_command_record_with_message_first_is_the_trigger() -> None:
    # Real shape: one record, <command-message> before <command-name> (13/13 measured).
    t = T()
    cmd = t.user(
        "<command-message>morning is running</command-message>\n<command-name>/morning</command-name>",
        "p1",
        source=None,
        origin=None,
    )
    t.user("Base directory for this skill: /x", "p1", source=None, origin=None, isMeta=True)
    t.assistant("m1", "report", "end_turn")
    e = exchange_at(t, "m1")
    assert e.inputs == ()
    assert e.trigger is not None and (e.trigger.trigger_class, e.trigger.record_id) == ("slash-command", cmd)
