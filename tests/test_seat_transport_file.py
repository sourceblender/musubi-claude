"""A seat that owns its identity can take its transport from its own credential file.

``cc-start`` exports ``MUSUBI_API_URL`` and ``MUSUBI_TOKEN`` before it execs the
harness, and ``apply_seat_environment`` drops the per-user plugin options the
moment ``MUSUBI_ACTOR`` is set. Any host that starts Claude Code WITHOUT that
launcher — the desktop app, cron, launchd, a future harness — therefore got a
seat that claimed identity and had no transport at all: the plugin reported
``MUSUBI_API_URL is not configured`` while the keychain values it had just
discarded were sitting right there (Shiori, 2026-10-26, Claude desktop app).

So a seat may also keep its transport in ``~/.musubi/musubi-mcp-<actor>.env``,
0600, which is where this fleet already writes it. Only the two transport keys
are read from it: identity stays env-or-config.json, all-or-nothing, per
CONTRIBUTING rule 3.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from musubi_harness.plugin_runtime import PluginRuntime

from scripts.musubi_claude_runtime import (
    STATE_NAME,
    apply_plugin_settings,
    read_seat_transport,
    seat_transport_path,
)

OPTION = "CLAUDE_PLUGIN_OPTION_"
OTHER_SEAT = {
    OPTION + "ACTOR": "aoi",
    OPTION + "SEAT": "voice",
    OPTION + "MUSUBI_URL": "https://other.example",
    OPTION + "MUSUBI_TOKEN": "other.seats.token",
}
SEAT = {"MUSUBI_ACTOR": "shiori", "MUSUBI_PRESENCE": "shiori/command-chair", "MUSUBI_ZONE": "home"}
FILE_URL = "https://from-the-file.example"
FILE_TOKEN = "file.seat.token"


@pytest.fixture
def venv(tmp_path: Path) -> Path:
    """A plugin venv bin with the harness and its bundled HTTP client."""
    bin_dir = tmp_path / "plugin-venv" / "bin"
    bin_dir.mkdir(parents=True)
    for name in ("python", "musubi-harness", "musubi-memory-data"):
        (bin_dir / name).write_text("#!/bin/sh\n")
        (bin_dir / name).chmod(0o755)
    return bin_dir


def write_seat_file(home: Path, actor: str, body: str, mode: int = 0o600) -> Path:
    path = home / ".musubi" / f"musubi-mcp-{actor}.env"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(mode)
    return path


def resolve(env: dict[str, str], venv: Path, home: Path, data: Path) -> tuple[str, dict[str, str], str]:
    """Apply settings as the plugin does at import, then ask the harness what it resolves."""
    data.mkdir(parents=True, exist_ok=True)
    (data / "config.json").write_text(json.dumps({"memory_data_bin": "/fleet-tools/bin/memory-data"}))
    full = {"HOME": str(home), **env}
    with patch.dict(os.environ, full, clear=True), patch("sys.executable", str(venv / "python")):
        source = apply_plugin_settings()
        os.environ["PLUGIN_DATA"] = str(data)
        runtime = PluginRuntime(STATE_NAME)
        return source, dict(os.environ), runtime.memory_data_bin(runtime.runtime_config())


def test_the_seat_file_supplies_the_transport_the_launcher_did_not(venv: Path, tmp_path: Path) -> None:
    home = tmp_path / "home"
    write_seat_file(home, "shiori", f"MUSUBI_API_URL={FILE_URL}\nMUSUBI_TOKEN={FILE_TOKEN}\n")
    source, env, memory_data = resolve({**OTHER_SEAT, **SEAT}, venv, home, tmp_path / "data")
    assert source == "environment"
    assert (env["MUSUBI_API_URL"], env["MUSUBI_TOKEN"]) == (FILE_URL, FILE_TOKEN)
    assert "other.seats.token" not in json.dumps(env)  # the other seat's option is still dropped
    # With a URL and a token the seat now gets the bundled client, as a launcher seat does.
    assert memory_data == str(venv / "musubi-memory-data")


def test_the_launcher_wins_over_the_file_key_by_key(venv: Path, tmp_path: Path) -> None:
    home = tmp_path / "home"
    write_seat_file(home, "shiori", f"MUSUBI_API_URL={FILE_URL}\nMUSUBI_TOKEN={FILE_TOKEN}\n")
    launcher = {**SEAT, "MUSUBI_TOKEN": "launcher.token"}
    _, env, _ = resolve(launcher, venv, home, tmp_path / "data")
    assert env["MUSUBI_TOKEN"] == "launcher.token"  # never overwritten
    assert env["MUSUBI_API_URL"] == FILE_URL  # the missing half is filled


def test_the_file_cannot_set_identity(venv: Path, tmp_path: Path) -> None:
    # CONTRIBUTING rule 3: identity is env or config.json, all-or-nothing. A
    # credential file that could name an actor would be a way to become someone else.
    home = tmp_path / "home"
    body = "\n".join(
        [
            "MUSUBI_ACTOR=aoi",
            "MUSUBI_PRESENCE=aoi/voice",
            "MUSUBI_ZONE=work",
            "MUSUBI_DELIVERY_MODE=shadow",
            f"MUSUBI_API_URL={FILE_URL}",
            f"MUSUBI_TOKEN={FILE_TOKEN}",
        ]
    )
    write_seat_file(home, "shiori", body)
    _, env, _ = resolve(SEAT, venv, home, tmp_path / "data")
    assert (env["MUSUBI_ACTOR"], env["MUSUBI_PRESENCE"], env["MUSUBI_ZONE"]) == ("shiori", "shiori/command-chair", "home")
    assert "MUSUBI_DELIVERY_MODE" not in env
    assert env["MUSUBI_TOKEN"] == FILE_TOKEN  # transport did load


def test_a_file_others_can_read_is_refused(venv: Path, tmp_path: Path) -> None:
    # Loading a token out of a world-readable file would silently endorse it.
    # Refusing changes nothing for anyone (before this, nothing read the file at
    # all) and the reason is reported, so it is fail-closed without a regression.
    home = tmp_path / "home"
    path = write_seat_file(home, "shiori", f"MUSUBI_TOKEN={FILE_TOKEN}\n", mode=0o644)
    _, env, _ = resolve(SEAT, venv, home, tmp_path / "data")
    assert "MUSUBI_TOKEN" not in env
    assert read_seat_transport(path) == ({}, "refused_mode")
    # An absent token proves nothing on its own — it is also what a seat with the
    # whole feature switched off looks like. The SAME file at 0600 must load, or
    # this test is measuring the wiring instead of the mode.
    path.chmod(0o600)
    _, tightened, _ = resolve(SEAT, venv, home, tmp_path / "data")
    assert tightened["MUSUBI_TOKEN"] == FILE_TOKEN


@pytest.mark.parametrize("actor", ["../../etc/shadow", "..", "a/b", "", "Shiori"])
def test_an_actor_that_is_not_a_plain_name_selects_no_file(actor: str, tmp_path: Path) -> None:
    # The actor comes from the environment and lands in a path; it is validated here
    # and not on the trust that something upstream already did it.
    assert seat_transport_path({"MUSUBI_ACTOR": actor}, home=tmp_path) is None


def test_the_path_is_the_seats_own_file_under_dot_musubi(tmp_path: Path) -> None:
    path = seat_transport_path({"MUSUBI_ACTOR": "shiori"}, home=tmp_path)
    assert path == tmp_path / ".musubi" / "musubi-mcp-shiori.env"


def test_home_comes_from_the_mapping_before_the_process(tmp_path: Path) -> None:
    # Callers that hold a constructed env (the thoughts binding, every test) must
    # resolve against THAT env. Otherwise a test with a fabricated actor silently
    # reads whatever credential file the developer has for a real seat of that name.
    env = {"MUSUBI_ACTOR": "shiori", "HOME": str(tmp_path / "stated")}
    assert seat_transport_path(env) == tmp_path / "stated" / ".musubi" / "musubi-mcp-shiori.env"


def test_live_thoughts_configures_a_seat_whose_token_came_from_the_file(tmp_path: Path) -> None:
    # The thoughts binding reads a raw env and never goes through the runtime, so
    # it needs the same resolution or a file-transport seat streams nothing.
    from scripts import musubi_claude_thoughts as thoughts

    home = tmp_path / "home"
    write_seat_file(home, "bob", f"MUSUBI_API_URL={FILE_URL}\nMUSUBI_TOKEN={FILE_TOKEN}\n")
    env = {
        "HOME": str(home),
        "MUSUBI_ACTOR": "bob",
        "MUSUBI_PRESENCE": "bob/chair",
        "MUSUBI_DELIVERY_MODE": "verified",
        "CLAUDE_PLUGIN_DATA": str(tmp_path / "data"),
        OPTION + "THOUGHT_SOURCES": "alice/laptop",
    }
    assert thoughts.write_config(env) == "written"
    written = json.loads((tmp_path / "data" / "monitor" / "stream.bob__chair.json").read_text())
    assert (written["url"], written["token"]) == (FILE_URL, FILE_TOKEN)


def test_an_explicit_file_override_is_honoured(venv: Path, tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere.env"
    elsewhere.write_text(f"MUSUBI_API_URL={FILE_URL}\nMUSUBI_TOKEN={FILE_TOKEN}\n")
    elsewhere.chmod(0o600)
    env_in = {**SEAT, "MUSUBI_SEAT_ENV_FILE": str(elsewhere)}
    _, env, _ = resolve(env_in, venv, tmp_path / "home", tmp_path / "data")
    assert (env["MUSUBI_API_URL"], env["MUSUBI_TOKEN"]) == (FILE_URL, FILE_TOKEN)


def test_no_file_leaves_the_seat_exactly_as_it_was(venv: Path, tmp_path: Path) -> None:
    # The pre-existing contract: a seat without a token keeps the configured client.
    _, env, memory_data = resolve({**OTHER_SEAT, **SEAT}, venv, tmp_path / "home", tmp_path / "data")
    assert "MUSUBI_TOKEN" not in env and "MUSUBI_API_URL" not in env
    assert memory_data == "/fleet-tools/bin/memory-data"


def test_a_seatless_install_never_reads_a_file(venv: Path, tmp_path: Path) -> None:
    # No MUSUBI_ACTOR in env: this is a /config install, and its options still apply.
    home = tmp_path / "home"
    write_seat_file(home, "aoi", f"MUSUBI_TOKEN={FILE_TOKEN}\n")
    settings = {**OTHER_SEAT, OPTION + "ZONE": "home"}
    source, env, _ = resolve(settings, venv, home, tmp_path / "data")
    assert source == "settings"
    assert env["MUSUBI_TOKEN"] == "other.seats.token"


@pytest.mark.parametrize(
    "body",
    [
        'MUSUBI_API_URL="https://quoted.example"\n',
        "export MUSUBI_API_URL=https://quoted.example\n",
        "# a comment\n\n  MUSUBI_API_URL = https://quoted.example  \n",
        "MUSUBI_API_URL='https://quoted.example'\nIRRELEVANT=$(rm -rf /)\n",
    ],
)
def test_the_file_is_parsed_and_never_evaluated(body: str, tmp_path: Path) -> None:
    path = write_seat_file(tmp_path, "shiori", body)
    values, why = read_seat_transport(path)
    assert values == {"MUSUBI_API_URL": "https://quoted.example"}
    assert why == "read"


def test_an_absent_file_is_reported_as_absent(tmp_path: Path) -> None:
    assert read_seat_transport(tmp_path / "nope.env") == ({}, "absent")


def test_health_names_where_the_transport_came_from_without_printing_it(tmp_path: Path) -> None:
    # A transport that appears out of a file has to be visible, or the next person
    # debugging this seat cannot tell a loaded file from a lucky launcher.
    home = tmp_path / "home"
    write_seat_file(home, "shiori", f"MUSUBI_API_URL={FILE_URL}\nMUSUBI_TOKEN={FILE_TOKEN}\n")
    script = Path(__file__).resolve().parents[1] / "scripts" / "musubi-claude-health"
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "PYTHONPATH": str(script.parent),
        "CLAUDE_PLUGIN_DATA": str(tmp_path / "data"),
        "MUSUBI_ACTOR": "shiori",
        "MUSUBI_PRESENCE": "shiori/command-chair",
        "MUSUBI_ZONE": "home",
    }
    done = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, env=env, timeout=30)
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)["transport_source"] == "seat_env_file"
    assert FILE_TOKEN not in done.stdout  # the source, never the credential


def test_session_start_points_at_the_file_when_that_is_where_the_token_came_from(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from tests.test_token_presence import VOICE, load

    write_seat_file(tmp_path / "home", "aoi", f"MUSUBI_API_URL={FILE_URL}\nMUSUBI_TOKEN={VOICE}\n")
    module = load(monkeypatch, tmp_path, None)  # no launcher token: the file is the only source
    warning = module.token_warning() or ""
    assert module.seat_transport_source == "seat_env_file"
    # The path, not the <actor> pattern: which file a process reads depends on
    # MUSUBI_ACTOR, MUSUBI_SEAT_ENV_FILE and HOME, so the pattern is not actionable.
    expected = str(tmp_path / "home" / ".musubi" / "musubi-mcp-aoi.env")
    assert module.seat_transport_file == expected
    assert expected in warning and "cc-start" not in warning


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("MUSUBI_TOKEN=abc # note", "abc"),  # an inline comment is not part of the credential
        ("MUSUBI_TOKEN=abc\t# note", "abc"),
        ('MUSUBI_TOKEN="abc # note"', "abc # note"),  # quoted: the value is what was quoted
        ("MUSUBI_API_URL=https://x/#frag", "https://x/#frag"),  # no space, so not a comment
    ],
)
def test_an_inline_comment_ends_an_unquoted_value(line: str, expected: str, tmp_path: Path) -> None:
    path = write_seat_file(tmp_path, "shiori", line + "\n")
    values, _ = read_seat_transport(path)
    assert list(values.values()) == [expected]


def test_the_mode_check_is_on_the_symlink_target(tmp_path: Path) -> None:
    # stat() follows symlinks, so a link to a 0600 file loads and a link to a 0644 one
    # is refused. That is the behaviour we want — the credential's own mode decides —
    # and it is asserted so it stays deliberate rather than incidental.
    real = tmp_path / "real.env"
    real.write_text(f"MUSUBI_TOKEN={FILE_TOKEN}\n")
    real.chmod(0o600)
    link = tmp_path / "link.env"
    link.symlink_to(real)
    assert read_seat_transport(link) == ({"MUSUBI_TOKEN": FILE_TOKEN}, "read")
    real.chmod(0o644)
    assert read_seat_transport(link) == ({}, "refused_mode")


def test_a_directory_is_not_a_credential_file(tmp_path: Path) -> None:
    assert read_seat_transport(tmp_path) == ({}, "not_a_file")
