"""Claude transcript -> exchange, per musubi-harness docs/exchange-identity.md.

An exchange is one terminal assistant answer plus the ordered tty-voiced input
since the preceding terminal answer in the same context, or a classified trigger
when that span is empty. This module is pure: it reads parsed transcript records
and returns an Exchange or raises ExchangeError with a named reason. It never
builds an envelope and never talks to the harness.

Claude facts this rests on (measured 2026-09-28 over both Claude seats):
- promptId names a TURN. It rides on every type=user record in the turn, human
  and machine alike, so it cannot be the exchange identity.
- Every promptId has exactly one terminal answer (12,168/12,168), reached from
  the assistant records by following parentUuid back to a record that carries
  the promptId (assistant records have no promptId of their own).
- In 2,945/2,945 captured Stops since 2026-09-20 that answer's joined text
  blocks equal the Stop payload's last_assistant_message exactly. That equality
  is the binding check here; the answer TEXT still comes from the Stop event.
- /clear opens a new transcript with a new sessionId (16/16), so one transcript
  is one context and a span can never fold across a reset.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

TERMINAL_STOP_REASONS = frozenset({"end_turn", "stop_sequence", "refusal"})
# Input channels, not speakers. typed/queued are the terminal; sdk is an SDK or IDE
# (VS Code) caller; suggestion_accepted is a suggested prompt a person accepted. All four
# were measured on real transcripts (2026-09-28); the last two were declining as
# input_unclassified until the corpus run found 27 of them.
TTY_PROMPT_SOURCES = frozenset({"typed", "queued", "sdk", "suggestion_accepted"})

# Closed set. A class outside it is a bug in this module, not a new kind of input.
TRIGGER_TASK = "task-notification"
TRIGGER_PEER = "peer-message"
TRIGGER_SCHEDULED = "scheduled"
TRIGGER_SLASH = "slash-command"

COMMAND_PREFIX = "<command-name>"
# Skill commands write ONE record whose tags come in the other order:
# <command-message>…</command-message><command-name>…</command-name>. Measured
# 13/13 on 2026-09-28; it is the command record itself, not context.
COMMAND_MESSAGE_PREFIX = "<command-message>"
STDOUT_PREFIX = "<local-command-stdout>"

# Bounds for walking parentUuid chains; a real chain is far shorter, and a cycle in
# a corrupt transcript must end in a decline rather than a hang.
_CHAIN_LIMIT = 10_000


class ExchangeError(Exception):
    """The transcript cannot identify this exchange. The message is the reason code."""


@dataclass(frozen=True)
class InputRecord:
    record_id: str
    text: str


@dataclass(frozen=True)
class Trigger:
    trigger_class: str
    record_id: str
    text: str


@dataclass(frozen=True)
class Exchange:
    session_id: str
    answer_id: str
    inputs: tuple[InputRecord, ...]
    trigger: Trigger | None
    is_sidechain: bool


def _text(record: dict[str, Any]) -> str:
    content = (record.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(block.get("text", "") for block in content if isinstance(block, dict) and block.get("type") == "text")
    return ""


def _is_tool_result(record: dict[str, Any]) -> bool:
    content = (record.get("message") or {}).get("content")
    return isinstance(content, list) and any(isinstance(block, dict) and block.get("type") == "tool_result" for block in content)


def _origin_kind(record: dict[str, Any]) -> str | None:
    origin = record.get("origin")
    if isinstance(origin, dict):
        kind = origin.get("kind")
        return kind if isinstance(kind, str) else None
    return None


# Classification results for a type=user record.
TTY = "tty"
MACHINE = "machine"  # context only: never voice, never a trigger
STDOUT = "stdout"  # machine, but only once proven paired to a command
UNCLASSIFIED = "unclassified"


def classify_user(record: dict[str, Any]) -> tuple[str, str | None]:
    """Return (kind, trigger_class) for a type=user record, from host STRUCTURE.

    promptSource wins: a typed or queued record is tty-voiced whatever its text
    says. A peer who writes "<local-command-stdout>" or "/clear" in a message is
    still speaking (the contract's literal-marker case). Prefixes are consulted
    only on records that carry no promptSource at all.
    """
    if _is_tool_result(record) or record.get("isCompactSummary") is True:
        return MACHINE, None
    source = record.get("promptSource")
    if source in TTY_PROMPT_SOURCES:
        return TTY, None
    kind = _origin_kind(record)
    if kind == "task-notification":
        return MACHINE, TRIGGER_TASK
    if kind == "peer" and source == "system":
        return MACHINE, TRIGGER_PEER
    if record.get("turnOrigin") == "scheduled":
        return MACHINE, TRIGGER_SCHEDULED
    if record.get("isMeta") is True:
        # Hook context and system reminders ride as isMeta alongside real input.
        return MACHINE, None
    if source is None:
        text = _text(record).lstrip()
        if text.startswith(COMMAND_PREFIX) or text.startswith(COMMAND_MESSAGE_PREFIX):
            return MACHINE, TRIGGER_SLASH
        if text.startswith(STDOUT_PREFIX):
            return STDOUT, None
    return UNCLASSIFIED, None


def _terminal_answers(records: list[dict[str, Any]]) -> list[tuple[int, str, dict[str, Any]]]:
    """(position, message.id, last line) per distinct terminal answer, transcript order.

    One model message may span several lines; it counts once, at the line that
    carries its terminal stop_reason.
    """
    seen: set[str] = set()
    out: list[tuple[int, str, dict[str, Any]]] = []
    for position, record in enumerate(records):
        if record.get("type") != "assistant":
            continue
        message = record.get("message") or {}
        message_id = message.get("id")
        if not isinstance(message_id, str) or message_id in seen:
            continue
        if message.get("stop_reason") in TERMINAL_STOP_REASONS:
            seen.add(message_id)
            out.append((position, message_id, record))
    return out


def _answer_text(records: list[dict[str, Any]], message_id: str) -> str:
    parts: list[str] = []
    for record in records:
        if record.get("type") != "assistant":
            continue
        message = record.get("message") or {}
        if message.get("id") != message_id:
            continue
        for block in message.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
    return "\n\n".join(parts)


def _prompt_id_of(record: dict[str, Any], by_uuid: dict[str, dict[str, Any]]) -> str | None:
    current: dict[str, Any] | None = record
    for _ in range(_CHAIN_LIMIT):
        if current is None:
            return None
        prompt_id = current.get("promptId")
        if isinstance(prompt_id, str) and prompt_id:
            return prompt_id
        current = by_uuid.get(current.get("parentUuid"))
    raise ExchangeError("parent_chain_unbounded")


def resolve_anchor(records: list[dict[str, Any]], prompt_id: str, answer_text: str) -> str:
    """The message.id of the one terminal answer this Stop closes.

    Declines rather than guesses: no terminal answer for the prompt yet, several of
    them, or an answer whose transcript text disagrees with the Stop payload.
    """
    by_uuid = {r["uuid"]: r for r in records if isinstance(r.get("uuid"), str)}
    matches = [message_id for _, message_id, record in _terminal_answers(records) if _prompt_id_of(record, by_uuid) == prompt_id]
    if not matches:
        raise ExchangeError("answer_not_in_transcript")
    if len(matches) > 1:
        raise ExchangeError("answer_ambiguous")
    anchor = matches[0]
    if _answer_text(records, anchor) != answer_text:
        raise ExchangeError("answer_text_mismatch")
    return anchor


def exchange_for(records: list[dict[str, Any]], answer_id: str) -> Exchange:
    """Build the exchange that ends at terminal answer `answer_id`."""
    every = _terminal_answers(records)
    anchor_entry = next((entry for entry in every if entry[1] == answer_id), None)
    if anchor_entry is None:
        raise ExchangeError("answer_not_in_transcript")
    sidechain = bool(anchor_entry[2].get("isSidechain"))
    # The span boundary is the previous answer on the SAME chain. A subagent's answer
    # in the main file must not cut the main exchange's span short, or vice versa.
    answers = [entry for entry in every if bool(entry[2].get("isSidechain")) == sidechain]
    index = next(i for i, (_, mid, _) in enumerate(answers) if mid == answer_id)
    end, _, anchor = answers[index]
    start = answers[index - 1][0] + 1 if index > 0 else 0
    session_id = anchor.get("sessionId")
    if not isinstance(session_id, str) or not session_id:
        raise ExchangeError("session_id_missing")

    inputs: list[InputRecord] = []
    triggers: list[Trigger] = []
    commands: set[str] = set()
    for record in records[start:end]:
        if record.get("type") != "user" or bool(record.get("isSidechain")) != sidechain:
            continue
        if record.get("sessionId") not in (None, session_id):
            raise ExchangeError("session_mismatch_in_span")
        record_id = record.get("uuid")
        kind, trigger_class = classify_user(record)
        if kind == TTY:
            if not isinstance(record_id, str):
                raise ExchangeError("input_record_id_missing")
            inputs.append(InputRecord(record_id, _text(record)))
        elif kind == STDOUT:
            # Machine output only when its parent IS a command record in this span.
            if record.get("parentUuid") not in commands:
                raise ExchangeError("stdout_unpaired")
        elif kind == UNCLASSIFIED:
            raise ExchangeError("input_unclassified")
        if trigger_class is not None:
            if not isinstance(record_id, str):
                raise ExchangeError("trigger_record_id_missing")
            if trigger_class == TRIGGER_SLASH:
                commands.add(record_id)
            triggers.append(Trigger(trigger_class, record_id, _text(record)))

    trigger: Trigger | None = None
    if not inputs:
        if not triggers:
            raise ExchangeError("no_eligible_input")
        if len(triggers) > 1:
            # Several machine triggers and no voice: which one the answer answers is
            # not something the transcript states. Decline rather than pick.
            raise ExchangeError("trigger_ambiguous")
        trigger = triggers[0]
    return Exchange(session_id, answer_id, tuple(inputs), trigger, sidechain)


def exchange_id(session_id: str, answer_id: str) -> str:
    """exchange.v1:<source>:<session>:<answer_id>, per the contract and EVENT_RE."""
    return f"exchange.v1:claude-code:{session_id}:{answer_id}"


def iter_dicts(records: Iterable[Any]) -> list[dict[str, Any]]:
    """Drop the reader's skip markers and anything that is not a record object."""
    return [r for r in records if isinstance(r, dict)]


# --- Envelope projection (harness >= 1.7.0) -------------------------------------------

EXCHANGE_VERSION = "v1"
_METADATA_VALUE_LIMIT = 1024  # musubi_harness.core: metadata string values <= 1 KB


def canonical_json(value: Any) -> str:
    """The contract's shared preimage: exact, non-ASCII-escaped, no whitespace.

    Same settings as the Codex adapter (Yua, 2026-09-28). ensure_ascii=False matters:
    the default escapes an em dash and the two hosts would stop agreeing on a digest.
    """
    import json

    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _sha256(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def exchange_metadata(exchange: Exchange, prompt_id: str) -> dict[str, str]:
    """Scalar metadata that makes replay collision-exact under the harness's check.

    input_record_texts_sha256 covers each record's exact text in order, so the same record
    ids with text repartitioned between them are a collision, not a quiet replay.
    Record ids ride verbatim while they fit the harness's 1 KB value limit; past it,
    their digest plus a count, with the exact list rederivable from the transcript.
    """
    ids = [record.record_id for record in exchange.inputs]
    texts = [record.text for record in exchange.inputs]
    metadata: dict[str, str] = {
        "exchange_version": EXCHANGE_VERSION,
        "session_id": exchange.session_id,
        "prompt_id": prompt_id,
        "answer_id": exchange.answer_id,
        # Key names and overflow shape match the Codex adapter (Yua, 846260e), so one
        # reader can rely on them across hosts.
        "input_record_texts_sha256": _sha256(canonical_json(texts)),
    }
    encoded_ids = canonical_json(ids)
    if len(encoded_ids.encode("utf-8")) <= _METADATA_VALUE_LIMIT:
        metadata["input_record_ids"] = encoded_ids
    else:
        metadata["input_record_ids_sha256"] = _sha256(encoded_ids)
        metadata["input_record_count"] = str(len(ids))
    return metadata


def input_fields(exchange: Exchange) -> dict[str, Any]:
    """user_text and the 1.7 trigger fields for this exchange.

    Voice: the inputs' exact texts in order, joined by a blank line; no trigger keys
    at all, so the payload is byte-identical to a pre-1.7 voice envelope.
    Trigger: user_text is empty and the machine text lives only in trigger_text, which
    the harness renders as "Trigger (<class>):", never as "User:".
    """
    if exchange.trigger is None:
        return {"user_text": "\n\n".join(record.text for record in exchange.inputs)}
    return {
        "user_text": "",
        "input_kind": "trigger",
        "trigger_class": exchange.trigger.trigger_class,
        "trigger_record_id": exchange.trigger.record_id,
        "trigger_text": exchange.trigger.text,
    }
