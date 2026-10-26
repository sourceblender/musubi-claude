"""A seat's own transport credentials, read from its own file.

``cc-start`` exports ``MUSUBI_API_URL`` and ``MUSUBI_TOKEN`` before it execs the
harness, and a process that sets ``MUSUBI_ACTOR`` has had the per-user plugin
options dropped (``musubi_claude_runtime.apply_seat_environment``). Every host
that starts Claude Code WITHOUT that launcher — the desktop app, cron, launchd —
was therefore left owning an identity with no transport at all, reporting
``MUSUBI_API_URL is not configured`` while the options it had just discarded sat
in the keychain (Shiori, 2026-10-26, Claude desktop app).

So a seat may also keep its transport in ``~/.musubi/musubi-mcp-<actor>.env``,
which is where this fleet's token minting already writes one per seat.

This module has no import side effects on purpose: both the runtime (every hook
and the MCP server) and the live-thoughts binding need it, and the thoughts
binding must not pull in the runtime's import-time identity mapping.
"""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Mapping, MutableMapping
from pathlib import Path

# The ONLY keys a seat's credential file may set. Identity stays env-or-config.json,
# all-or-nothing (CONTRIBUTING rule 3): a credential file that could name an actor
# would be a way to become someone else, so the allowlist is the mechanism here,
# not a convention.
SEAT_TRANSPORT_KEYS = ("MUSUBI_API_URL", "MUSUBI_TOKEN")
# The actor arrives from the environment and lands inside a path. Validate it here
# rather than trusting that something upstream already did.
_PLAIN_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?$")


def seat_transport_path(env: Mapping[str, str], home: Path | None = None) -> Path | None:
    """This seat's own credential file, or None when no safe path names one.

    ``MUSUBI_SEAT_ENV_FILE`` overrides it outright. Otherwise the file is
    ``~/.musubi/musubi-mcp-<actor>.env``. Home comes from the mapping that was
    passed in before the process's own, so a caller holding a constructed env
    (the thoughts binding, every test) resolves against that env and not against
    whatever credential files the developer happens to have.

    An actor that is not a plain lowercase name selects nothing: ".." would walk
    out of the directory, and a name is not a place to be clever.
    """
    override = env.get("MUSUBI_SEAT_ENV_FILE", "").strip()
    if override:
        return Path(override).expanduser()
    actor = env.get("MUSUBI_ACTOR", "").strip()
    if not _PLAIN_NAME.match(actor) or ".." in actor:
        return None
    if home is None:
        stated = env.get("HOME", "").strip()
        home = Path(stated) if stated else Path.home()
    return home / ".musubi" / f"musubi-mcp-{actor}.env"


def read_seat_transport(path: Path) -> tuple[dict[str, str], str]:
    """Read MUSUBI_API_URL / MUSUBI_TOKEN out of a dotenv, and say why if none.

    Parsed, never evaluated: one ``KEY=value`` per line, ``#`` comments and an
    optional ``export`` prefix, matching quotes stripped. Any other key is
    ignored, so the file cannot reach identity or any other setting.

    A file that group or others can read is REFUSED on POSIX: loading a token out
    of it would silently endorse the mode. Refusing regresses nothing — before
    this, nothing read the file at all — and the reason is reported, so a 0644
    file becomes a named finding instead of a quiet credential leak. Mode bits do
    not carry the same meaning off POSIX, so the check is POSIX-only.
    """
    try:
        info = path.stat()
    except OSError:
        return {}, "absent"
    if not stat.S_ISREG(info.st_mode):
        return {}, "not_a_file"
    if os.name == "posix" and info.st_mode & 0o077:
        return {}, "refused_mode"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}, "unreadable"
    found: dict[str, str] = {}
    for line in text.splitlines():
        item = line.strip()
        if not item or item.startswith("#"):
            continue
        key, sep, value = item.removeprefix("export ").partition("=")
        if not sep or key.strip() not in SEAT_TRANSPORT_KEYS:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        else:
            # Unquoted: an inline comment ends the value, so `TOKEN=abc # note` does
            # not carry " # note" into the credential. Only after whitespace, so a '#'
            # inside a value (a URL fragment) survives.
            value = value.split(" #", 1)[0].split("\t#", 1)[0].strip()
        if value:
            found[key.strip()] = value
    return found, "read" if found else "no_keys"


def apply_seat_transport_file(env: MutableMapping[str, str], home: Path | None = None) -> str:
    """Fill the transport this seat's launcher did not set from the seat's own file.

    The launcher still wins, key by key: this only fills what is missing, so a
    stale file can never displace a live export. A token in the file that belongs
    to another presence is not caught here — SessionStart's token check names it
    on the first turn, wherever the token came from.

    Returns the source label: "environment" (the launcher supplied both),
    "seat_env_file", "seat_env_file_partial", or why nothing loaded ("absent",
    "refused_mode", "unreadable", "no_keys", "none").
    """
    missing = [key for key in SEAT_TRANSPORT_KEYS if not env.get(key, "").strip()]
    if not missing:
        return "environment"
    path = seat_transport_path(env, home)
    if path is None:
        return "none"
    values, why = read_seat_transport(path)
    filled = {key: values[key] for key in missing if values.get(key)}
    if not filled:
        return why if why != "read" else "no_keys"
    env.update(filled)
    complete = all(env.get(key, "").strip() for key in SEAT_TRANSPORT_KEYS)
    return "seat_env_file" if complete else "seat_env_file_partial"
