"""Live thoughts: messages from other agents, delivered into this session.

Musubi's thought plane carries agent-to-agent messages and serves them live on
``GET /v1/thoughts/stream`` (server-sent events, a ping every 30 s, replay from
``Last-Event-ID``). This module is the client behind the plugin's monitor:
Claude Code runs it for the whole interactive session, and every line it prints
reaches Claude as a notification, without anyone asking.

Output contract (one line per thought, nothing else except rare status lines)::

    musubi thought from <presence> [<thought_id>] (untrusted data): <text>

The text is the thought's own content, flattened to one line and capped. It is
labelled untrusted because it is: another agent wrote it, and a thought that
says "run this" is a message about something, never an instruction to follow.

Monitor processes receive no plugin settings, so the connection comes from a
small JSON file (``--config``) that the plugin's SessionStart hook writes
(``write_config``): url, token, namespace and presence, nothing else, in a
0700 directory, created 0600 with no window, only in ``verified`` mode. The
monitor reads it ONCE at start (waiting briefly for SessionStart to write it)
and keeps the values in memory, so another session's SessionEnd removing the
file can't deafen a running stream. It never follows redirects, so the bearer
token can't be forwarded elsewhere. Its own status lines carry only locally
generated text (never a URL, response body or exception text). It prints at
most one "unavailable" line per outage, and keeps ``Last-Event-ID`` across
reconnects so nothing is repeated or skipped.

Identity follows the same rule as every other hook (see
``musubi_claude_runtime.apply_seat_environment``): a seat whose launcher sets
``MUSUBI_ACTOR`` owns its identity AND transport, and the per-user plugin
options are ignored for it. Those options are shared by every seat under one OS
user, so reading them here would stream as whoever last set /config, with their
token. A seat's connection file is its own (``stream.<actor>__<seat>.json``
beside ``--config``), so one seat's SessionStart can't point another seat's
monitor at a different identity, and its SessionEnd removes only its own.

Musubi stores each thought in its sender's namespace (``<presence>/thought``)
and the stream matches a namespace exactly, so the monitor opens one stream per
watched sender (the ``thought_sources`` setting); the server filters each to
thoughts addressed to this presence or to ``all``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

READ_TIMEOUT = 75.0  # the server pings every 30 s
TEXT_CHARS = 300
BACKOFF = (1, 2, 5, 10, 30, 60)
CONFIG_WAIT_SECONDS = 15.0
_OPTION = "CLAUDE_PLUGIN_OPTION_"
_PRINT_LOCK = threading.Lock()
_PRESENCE = re.compile(r"^[a-z0-9][a-z0-9._-]*/[a-z0-9][a-z0-9._-]*$")
_OBJECT_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# C0/C1 controls, line/paragraph separators and bidi overrides: anything that
# could break the one-line notification or disguise what it says.
_CONTROLS = re.compile("[\x00-\x1f\x7f-\x9f\u2028\u2029\u202a-\u202e\u2066-\u2069]")
MAX_LINE_BYTES = 16 * 1024
MAX_EVENT_BYTES = 64 * 1024
_OVERLONG = "\x00overlong\n"


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        raise urllib.error.HTTPError(req.full_url, code, "redirect refused", headers, fp)


_OPENER = urllib.request.build_opener(_RefuseRedirects)


def load_config(path: Path) -> dict[str, Any] | None:
    """The connection file, or None if it is missing or incomplete."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    if not all(isinstance(raw.get(k), str) and raw[k].strip() for k in ("url", "token")):
        return None
    namespaces = raw.get("namespaces")
    if not isinstance(namespaces, list) or not namespaces or not all(isinstance(n, str) and n for n in namespaces):
        return None
    config: dict[str, Any] = {"url": raw["url"].strip(), "token": raw["token"].strip(), "namespaces": namespaces}
    if isinstance(raw.get("presence"), str):
        config["presence"] = raw["presence"]
    return config


def stream_url(config: dict[str, Any], namespace: str) -> str:
    base = str(config["url"]).rstrip("/")
    if not base.endswith("/v1"):
        base += "/v1"
    return f"{base}/thoughts/stream?" + urllib.parse.urlencode({"namespace": namespace})


def parse_events(lines: Iterable[str]) -> Iterator[dict[str, str]]:
    """Minimal SSE parser: yields {'event','id','data'} per dispatched event."""
    event: dict[str, str] = {}
    data: list[str] = []
    size = 0
    for raw in lines:
        line = raw.rstrip("\r\n")
        if not line:
            if data and size <= MAX_EVENT_BYTES:
                event["data"] = "\n".join(data)
                yield event
            event, data, size = {}, [], 0
            continue
        if line.startswith(":"):
            continue
        if raw == _OVERLONG:
            size = MAX_EVENT_BYTES + 1
            continue
        field, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if field == "data":
            size += len(value) + 1
            if size <= MAX_EVENT_BYTES:  # an oversized event is dropped whole
                data.append(value)
        elif field in ("event", "id"):
            event[field] = value


def format_thought(event: dict[str, str], own_presence: str | None, stream_presence: str | None = None) -> str | None:
    """One notification line for a thought event, or None to stay quiet.

    ``stream_presence`` is the owner of the namespace the event arrived on.
    Musubi takes ``from_presence`` from the request body and checks only the
    writer's scope on the namespace, so the namespace is the authority: a thought
    on ``yua/laptop/thought`` is from yua/laptop whatever it claims, and a
    different claim is shown as a claim.
    """
    if event.get("event", "message") != "thought":
        return None
    try:
        thought: Any = json.loads(event.get("data", ""))
    except json.JSONDecodeError:
        return None
    if not isinstance(thought, dict):
        return None
    claimed = thought.get("from_presence")
    claimed = claimed if isinstance(claimed, str) and _PRESENCE.fullmatch(claimed) else None
    sender = stream_presence or claimed
    if sender is None:
        return None  # a sender we cannot print safely is not shown at all
    if own_presence and sender == own_presence:
        return None  # our own outgoing thought echoed back
    if claimed != sender:
        sender += f" (claims to be {claimed})" if claimed else " (sender field invalid)"
    text = " ".join(_CONTROLS.sub(" ", str(thought.get("content") or "")).split())
    if not text:
        return None
    if len(text) > TEXT_CHARS:
        text = text[: TEXT_CHARS - 1] + "…"
    thought_id = next(
        (c for c in (thought.get("object_id"), event.get("id")) if isinstance(c, str) and _OBJECT_ID.fullmatch(c)),
        "?",
    )
    return f"musubi thought from {sender} [{thought_id}] (untrusted data): {text}"


def _lines(response: Any) -> Iterator[str]:
    """Lines of at most MAX_LINE_BYTES; the rest of an overlong line is discarded."""
    overlong = False
    while True:
        raw = response.readline(MAX_LINE_BYTES)
        if not raw:
            return
        complete = raw.endswith(b"\n")
        if not overlong:
            # A truncated line poisons the event it belongs to rather than
            # letting a partial field through.
            yield raw.decode("utf-8", errors="replace") if complete else _OVERLONG
        overlong = not complete


def wait_for_config(path: Path, *, wait: float = CONFIG_WAIT_SECONDS, sleep: Any = time.sleep) -> dict[str, Any] | None:
    """Read the connection file once, giving SessionStart a moment to write it."""
    deadline = time.monotonic() + wait
    started = time.time() - 30  # a file older than this session is a crash leftover
    while True:
        try:
            fresh = path.stat().st_mtime >= started
        except OSError:
            fresh = False
        config = load_config(path) if fresh else None
        if config is not None or time.monotonic() >= deadline:
            return config
        sleep(0.5)


def run(config: dict[str, Any], *, max_cycles: int | None = None, sleep: Any = time.sleep) -> int:
    """One stream per watched namespace, in threads; returns when all stop."""
    threads = [
        threading.Thread(target=run_one, args=(config, ns), kwargs={"max_cycles": max_cycles, "sleep": sleep}, daemon=True)
        for ns in config["namespaces"]
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return 0


def _emit(line: str) -> None:
    with _PRINT_LOCK:
        print(line, flush=True)


def run_one(config: dict[str, Any], namespace: str, *, max_cycles: int | None = None, sleep: Any = time.sleep) -> int:
    """Stream one namespace forever (Claude Code stops the monitor). ``max_cycles`` bounds tests."""
    last_id: str | None = None
    failures = 0
    announced_down = False
    cycles = 0
    url = stream_url(config, namespace)
    owner = namespace.removesuffix("/thought") if _PRESENCE.fullmatch(namespace.removesuffix("/thought")) else None
    while max_cycles is None or cycles < max_cycles:
        cycles += 1
        headers = {"Accept": "text/event-stream", "Authorization": f"Bearer {config['token']}"}
        if last_id:
            headers["Last-Event-ID"] = last_id
        try:
            request = urllib.request.Request(url, headers=headers)
            with _OPENER.open(request, timeout=READ_TIMEOUT) as response:
                if announced_down:
                    _emit(f"musubi thoughts: {namespace} reconnected")
                failures, announced_down = 0, False
                for event in parse_events(_lines(response)):
                    if event.get("id"):
                        last_id = event["id"]
                    line = format_thought(event, config.get("presence"), owner)
                    if line:
                        _emit(line)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                _emit(f"musubi thoughts: {namespace} refused (HTTP {int(exc.code)}); check the Musubi token and its read scope")
                return 0
            failures += 1
        except (OSError, ValueError):
            failures += 1
        if failures and not announced_down and failures >= 3:
            _emit(f"musubi thoughts: {namespace} unavailable; retrying quietly")
            announced_down = True
        sleep(BACKOFF[min(failures, len(BACKOFF) - 1)] if failures else 1)
    return 0


def _seat_owned(env: dict[str, str]) -> bool:
    return bool(env.get("MUSUBI_ACTOR", "").strip())


def config_path(folder: Path, env: dict[str, str]) -> Path | None:
    """This process's connection file in ``folder``, or None for a seat whose presence is unusable.

    Without a seat launcher there is one file, as before. A seat gets its own, named
    by its presence, and never reads or removes the shared one.
    """
    if not _seat_owned(env):
        return folder / "stream.json"
    actor = env.get("MUSUBI_ACTOR", "").strip()
    presence = env.get("MUSUBI_PRESENCE", "").strip()
    if not _PRESENCE.fullmatch(presence) or presence.split("/", 1)[0] != actor:
        return None
    return folder / f"stream.{presence.replace('/', '__')}.json"


def _identity(env: dict[str, str]) -> tuple[str, str, str, str]:
    """(url, token, presence, delivery mode), resolved the way every other hook resolves it.

    A seat launcher that sets MUSUBI_ACTOR owns identity AND transport: the
    per-user options are never consulted for it, not even as a fallback, so a
    seat without its own token streams nothing rather than someone else's.
    """
    if _seat_owned(env):
        return (
            env.get("MUSUBI_API_URL", "").strip(),
            env.get("MUSUBI_TOKEN", "").strip(),
            env.get("MUSUBI_PRESENCE", "").strip(),
            env.get("MUSUBI_DELIVERY_MODE", "").strip(),
        )
    actor = env.get(_OPTION + "ACTOR", "").strip()
    seat = env.get(_OPTION + "SEAT", "").strip()
    return (
        env.get(_OPTION + "MUSUBI_URL", "").strip(),
        env.get(_OPTION + "MUSUBI_TOKEN", "").strip(),
        f"{actor}/{seat}" if actor and seat else "",
        env.get(_OPTION + "DELIVERY_MODE", "").strip(),
    )


def write_config(env: dict[str, str] | None = None) -> str:
    """SessionStart: write (or remove) the monitor's connection file. Returns a status word."""
    env = dict(os.environ) if env is None else env
    data = env.get("CLAUDE_PLUGIN_DATA", "").strip()
    if not data:
        return "no_data_dir"
    folder = Path(data).expanduser() / "monitor"
    target = config_path(folder, env)
    if target is None:
        return "disabled"
    url, token, presence, mode = _identity(env)
    if not (url and token and presence) or mode != "verified":
        target.unlink(missing_ok=True)
        return "disabled"
    # Musubi stores a thought in its SENDER's namespace (<presence>/thought) and the
    # stream matches that namespace exactly, so we watch each source's namespace.
    sources = [p.strip() for p in env.get(_OPTION + "THOUGHT_SOURCES", "").split(",") if p.strip()]
    sources = [p for p in dict.fromkeys(sources) if _PRESENCE.fullmatch(p) and p != presence][:20]
    if not sources:
        target.unlink(missing_ok=True)
        return "no_sources"
    payload = json.dumps({"url": url, "token": token, "namespaces": [f"{p}/thought" for p in sources], "presence": presence})
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(folder, 0o700)
    tmp = folder / f".stream.{os.getpid()}.tmp"
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)
    return "written"


def remove_config(env: dict[str, str] | None = None) -> None:
    """SessionEnd: remove this process's connection file (a running monitor already holds its copy)."""
    env = dict(os.environ) if env is None else env
    data = env.get("CLAUDE_PLUGIN_DATA", "").strip()
    if data:
        target = config_path(Path(data).expanduser() / "monitor", env)
        if target is not None:
            target.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stream Musubi thoughts as monitor lines.")
    parser.add_argument("--config", help="connection file written by the SessionStart hook")
    parser.add_argument("--write-config", action="store_true", help="SessionStart: write or remove the file")
    parser.add_argument("--remove-config", action="store_true", help="SessionEnd: remove the file")
    args = parser.parse_args(argv)
    try:
        if args.write_config:
            write_config()
            return 0
        if args.remove_config:
            remove_config()
            return 0
        if not args.config:
            return 0
        path = config_path(Path(args.config).parent, dict(os.environ))
        if path is None:
            return 0
        config = wait_for_config(path)
        if config is None:
            return 0  # shadow mode or no token: nothing to stream, stay silent
        return run(config)
    except KeyboardInterrupt:
        return 0
    except Exception as exc:  # noqa: BLE001 - class name only, never exception text
        print(f"musubi thoughts: stopped ({type(exc).__name__})", file=sys.stderr, flush=True)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
