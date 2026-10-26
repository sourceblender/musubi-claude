"""The identity a turn was prompted under, bound at UserPromptSubmit.

Claude Code re-reads the project's ``settings.json`` ``env`` block while a turn is
running, and each hook process gets the value current at its own spawn. Probed on
2026-10-26 (Claude Code 2.1.310) by flipping one variable from inside a running
turn: two reads in that same turn returned the old value and then the new one, with
no restart. Aoi reproduced the same thing through a tool call editing the file.

So ``MUSUBI_ACTOR`` can differ between the UserPromptSubmit that began a turn and
the Stop that captures it. Stop resolves identity from its own environment
(``musubi-claude-stop``'s ``_identity_config``), which means a turn prompted as one
seat could be enveloped as another — and, once a seat can also take its token from
its own credential file, delivered under that other seat's valid token, where the
server's scope check cannot see anything wrong because actor and token agree.

So the prompting identity is written down when the turn starts and compared when it
is captured. On a mismatch the capture is declined by name rather than attributed to
either seat. There is no guessing which one was meant: that is the user's
configuration changing under a turn, and the honest outcome is to drop that one
turn loudly.

This file is NOT actor-scoped — that is the point, since the actor is the thing
under suspicion — and it holds no conversation content and no credential: an
identity triple, a transport source label, and a timestamp. Nothing here talks to
the harness.
"""

from __future__ import annotations

import hashlib
import json
import os
import pwd
import time
from dataclasses import asdict, dataclass
from pathlib import Path

STATE_NAME = "musubi-claude"

# A binding older than this cannot belong to a live turn; a resumed session that
# never re-prompted is treated as unbound rather than compared against a stale row.
MAX_AGE_SECONDS = 24 * 3600
# Pruning is bounded work on a hook's critical path, never a full directory walk.
PRUNE_SCAN_LIMIT = 500

IDENTITY_CHANGED = "identity_changed_mid_turn"
TRANSPORT_CHANGED = "transport_changed_mid_turn"


@dataclass(frozen=True)
class Binding:
    session_id: str
    actor: str
    presence: str
    zone: str
    transport_source: str
    at: float


def fixed_root() -> Path:
    """The binding's root, chosen so the project's env block cannot move it.

    Aoi's second review found the first version of this guard storing its evidence
    somewhere the attack could reach: the binding lived under ``data_root()``, which
    the harness resolves from ``PLUGIN_DATA`` first, and ``PLUGIN_DATA`` comes from
    the same project ``env`` block as ``MUSUBI_ACTOR`` and is re-read mid-turn the
    same way. Flip both and Stop looks in the new folder, finds nothing, and
    "absent means proceed" lets the capture through under the new seat — into an
    outbox that is also under the new root, so the drain delivers it. She probed it
    with this file's own test helpers.

    So the root comes from the password database, not from the environment:
    ``$HOME`` is just another variable that block can set. Nothing here is
    configurable on purpose — a knob would re-open the hole. Tests substitute this
    function rather than an env var, for the same reason.

    A home that cannot be resolved at all falls back to ``Path.home()``: a binding
    in a movable place still catches every case except a deliberate co-flip, and is
    strictly better than no binding.
    """
    try:
        return Path(pwd.getpwuid(os.getuid()).pw_dir) / ".local" / "state" / STATE_NAME
    except (KeyError, OSError):
        return Path.home() / ".local" / "state" / STATE_NAME


def binding_dir(root: Path) -> Path:
    return root / "prompt-identity"


def binding_path(root: Path, session_id: str) -> Path:
    """One file per session, overwritten by each prompt.

    UserPromptSubmit's payload carries ``prompt`` and ``session_id`` and no prompt
    id, so the key is the session and the row is the LATEST prompt's identity. That
    is enough for the comparison that matters: the Stop of a turn is compared with
    the UserPromptSubmit that began that same turn. A pending candidate from an
    earlier prompt is deliberately NOT re-compared against this row — it would
    mismatch for the wrong reason. It does not need to be: it only exists because
    the check passed at its own Stop, and it lives under its own actor's pending
    directory, so a later drain under a different actor never sees it.
    """
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:24]
    return binding_dir(root) / f"{digest}.json"


def write(root: Path, binding: Binding) -> None:
    """Record the prompting identity, atomically and owner-only. Never raises for a caller to handle."""
    directory = binding_dir(root)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = directory / f".{os.getpid()}.tmp"
    tmp.write_text(json.dumps(asdict(binding), sort_keys=True), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, binding_path(root, binding.session_id))


def read(root: Path, session_id: str, now: float | None = None) -> Binding | None:
    """The identity this session's latest prompt was submitted under, or None.

    None means "nothing to compare against" — absent, unreadable, malformed, for
    another session, or too old. The caller then proceeds exactly as it did before
    there were bindings: a missing binding must not silently drop every capture on
    a seat whose UserPromptSubmit hook failed or never ran.
    """
    try:
        row = json.loads(binding_path(root, session_id).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(row, dict):
        return None
    try:
        bound = Binding(
            session_id=str(row["session_id"]),
            actor=str(row["actor"]),
            presence=str(row["presence"]),
            zone=str(row["zone"]),
            transport_source=str(row.get("transport_source") or ""),
            at=float(row["at"]),
        )
    except (KeyError, TypeError, ValueError):
        return None
    if bound.session_id != session_id:
        return None
    if (time.time() if now is None else now) - bound.at > MAX_AGE_SECONDS:
        return None
    return bound


def mismatch(bound: Binding, actor: str, presence: str, zone: str, transport_source: str) -> tuple[str | None, str | None]:
    """(blocking reason, advisory reason) comparing a capture's identity with its prompt's.

    An identity change BLOCKS: the envelope would name a seat that did not prompt
    the turn.

    A transport change alone does not, and that is a deliberate narrowing of the
    review's ask. The envelope is still correct, the actor still matches, and the
    benign direction exists and is common: a launcher export appearing mid-session
    moves the source from ``seat_env_file`` to ``environment`` with the same seat
    and the same token. Declining that would drop good captures. A token that does
    NOT belong to the bound actor is caught where it already was — the server's
    scope check, and SessionStart's token_warning. So the change is recorded by
    name and the capture proceeds.
    """
    if (bound.actor, bound.presence, bound.zone) != (actor, presence, zone):
        return IDENTITY_CHANGED, None
    if bound.transport_source and bound.transport_source != transport_source:
        return None, TRANSPORT_CHANGED
    return None, None


def prune(root: Path, now: float | None = None) -> int:
    """Drop expired bindings, scanning at most PRUNE_SCAN_LIMIT entries. Returns how many went."""
    directory = binding_dir(root)
    cutoff = (time.time() if now is None else now) - MAX_AGE_SECONDS
    removed = 0
    try:
        entries = sorted(directory.iterdir())[:PRUNE_SCAN_LIMIT]
    except OSError:
        return 0
    for path in entries:
        if path.suffix != ".json":
            continue
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
                removed += 1
        except OSError:
            continue
    return removed
