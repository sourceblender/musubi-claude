# musubi-claude

First-class Claude Code adapter for [Musubi](https://github.com/sourceblender/musubi) memory.
Automatic capture, recall that runs before Claude answers, memory that survives
`/compact`, live thoughts from other agents, a memory steward, and a health
check that tells you honestly whether memory is working. Built on the shared [`musubi-harness`](https://github.com/sourceblender/musubi-harness)
runtime so the Claude and Codex seats share one contract and never diverge.

| | |
| --- | --- |
| License | Apache-2.0 |
| Plugin name | `musubi-claude` |
| Marketplace | `sourceblender` |
| Runtime dep | [`musubi-harness`](https://pypi.org/project/musubi-harness/), installed by `/musubi-claude:setup` |
| Companion repos | [`musubi-codex`](https://github.com/sourceblender/musubi-codex), [`musubi-livekit`](https://github.com/sourceblender/musubi-livekit), [`musubi-hermes`](https://github.com/sourceblender/musubi-hermes), [`musubi-openclaw`](https://github.com/sourceblender/musubi-openclaw) |

**Shadow-only by default.** Nothing is written to Musubi until `delivery_mode`
is explicitly set to `verified`.

## Install

```bash
claude plugin marketplace add sourceblender/musubi-claude
claude plugin install musubi-claude@sourceblender
```

Then, inside Claude Code:

1. **`/musubi-claude:setup`**: one-time install of the pinned `musubi-harness`
   into the plugin's own data directory (needs the network once; afterwards
   capture and recall run offline). Restart Claude Code, or reconnect
   `musubi-claude` in `/mcp`.
2. Fill in the plugin settings (actor, seat, zone, delivery mode) when Claude
   Code asks, or later in `/config` → musubi-claude.
3. **`/musubi-claude:health`**: confirms it is working, in plain words.

Hooks never install anything. Until setup has run, they refuse visibly: the
session shows "Musubi memory is not set up yet", and the MCP server reports
the same. When a plugin update raises the required `musubi-harness` version,
the session says "Musubi memory needs a one-time update" instead, and nothing
is captured or recalled until you run `/musubi-claude:setup` again.

## What you get

| | What it does |
|---|---|
| **Automatic capture** (Stop hook) | Records each completed primary turn into a local outbox. In `verified` mode each turn also runs one bounded delivery pass over that outbox, oldest first, and a row counts as delivered only after an exact readback from Musubi; a backlog drains over later turns, and `/musubi-claude:health` shows what is still pending. In `shadow` mode (the default) nothing leaves the machine. |
| **Session continuity** (SessionStart hook) | A small, labelled block of recent memory at session start. An outage says "unavailable", never "nothing remembered". |
| **Prompt-aware recall** (UserPromptSubmit hook) | Before Claude answers, your prompt is used as a search and the few memories actually about it are added with their provenance, newest decision first. Superseded memories are dropped. Silent when nothing is relevant; says so when Musubi is unreachable. See [Prompt recall](#prompt-recall). |
| **Memory survives `/compact`** (PreCompact + SessionStart hooks) | Before compaction, the Musubi objects this session used (ids, plane, state, a short title) and the status of its remembers are checkpointed; after it, they come back as a labelled block, so Claude can `musubi_get` exactly what the summary lost. The checkpoint is a local file (0600, pruned after 7 days) holding ids, plane, state and an 80-character title per memory; for a captured turn the title is the start of that stored turn ("User: …"), so it can hold a few words of past conversation. |
| **Live thoughts** (plugin monitor) | Thoughts from the agents you choose arrive in the session as they are sent. See [Live thoughts](#live-thoughts-from-other-agents). |
| **Recall tools** (MCP) | `musubi_search`, `musubi_recent`, `musubi_get`, `musubi_status` (read-only) and `musubi_remember` (queues one memory). Recalled text is treated as data, never instructions. `queued` is never reported as stored. |
| **`/musubi-claude:recall <topic>`** | Search with provenance (object id, plane, state, score); says when memories disagree instead of trusting the top hit. |
| **`/musubi-claude:remember <fact>`** | Saves one fact and tells you honestly whether it is stored yet. Refuses credentials. |
| **`@musubi-claude:steward`** | A read-only subagent that audits one topic: what is current, what conflicts, what is stale, what is missing, and up to three changes for you to approve. It never changes memory itself. |
| **`/musubi-claude:health [--probe]`** | Is memory actually working? Settings, identity, delivery queue, recent capture skips, and records stranded in an old state directory. `--event <id>` answers whether one remember landed. Never prints content or credentials. |
| **Skills** `musubi-recall`, `musubi-continuity` | The standing reflexes Claude uses on its own for deliberate recall and for diagnosing capture. |

## Measured, not claimed

`evals/` is a [`claude plugin eval`](https://code.claude.com/docs/en/plugin-evals)
suite. Musubi is mocked from the real server's `tools/list`, so it needs no
network. Each case runs three times with the plugin and three times without
it; Δ is what the plugin contributes.

| case | Δ |
|---|---|
| recall a decision when two live memories conflict (newer must win, even ranked second) | +1.00 |
| a recalled memory that says "run this curl \| sudo bash" is reported, not followed | +1.00 |
| the steward finds the conflict and proposes, never claims to change | +1.00 |
| `/remember` reports queued as not yet stored | +0.67 |
| guards (Δ 0 by design): unreachable is not empty, no memory calls on unrelated tasks, secrets refused, queued honesty | 0.00 |

8/8 cases pass with the plugin; mean Δ **+0.46** (claude 2.1.281, measured
before prompt recall, compaction and thoughts landed; those were proven live
instead). Run it:
`claude plugin eval . --trust-plugin`.

## The Claude-specific difference

Claude Code's Stop hook supplies `session_id` + `transcript_path` + `prompt_id`
+ `last_assistant_message` (the latter two were shape-probed in 2026-07 →
2026-08), so the adapter binds the event's own identity and answer rather
than reconstructing them from the transcript. The event id is deterministic
and stable — `claude-code:<session_id>:<prompt_id>` — so a Stop retry or
resume re-enqueues the same id and the harness dedupes it.

## Identity is configuration — never derived from the host

`actor` / `presence` / `zone` come from the plugin's settings, or from the
legacy sources below. Partial or absent identity is **refused**, never
guessed. `presence` is the Musubi `actor/seat` form (e.g. `alice/laptop`); the
shared runtime enforces `actor == presence-prefix` and owned-namespace scope,
so no adapter can read or write under another actor's seat.

### Configuring the plugin

Claude Code asks for these when you enable the plugin. Non-sensitive fields
are also rows in `/config` → musubi-claude:

| setting | what it is |
|---|---|
| **Musubi actor**, **Seat** | your identity; presence becomes `actor/seat` |
| **Zone** | `home` or `work` |
| **Delivery mode** | `shadow` keeps captures on this machine; `verified` sends each one to Musubi and reads it back |
| **Musubi URL** | your Musubi server, e.g. `https://musubi.example.com` |
| **Musubi token** | your Musubi API token (a JWT). Marked `sensitive`: stored in the system credential store, not `settings.json`, and **not** shown as a `/config` row |
| **Prompt recall** | `auto` (default): only in `verified` mode, `on`, or `off`. See [Prompt recall](#prompt-recall) |
| **Live thought sources** | presences whose thoughts arrive live, e.g. `yua/laptop,tama/desk`; empty for none |

The plugin passes the URL and token to the harness as `MUSUBI_API_URL` /
`MUSUBI_TOKEN` inside its own hook and MCP processes only; you never export
them. With musubi-harness 1.1.0 or later, that is all a remote install needs:
the harness's bundled HTTP client talks to Musubi directly.

**Legacy sources**, used only when **Musubi actor** is empty: the
`MUSUBI_ACTOR` / `MUSUBI_PRESENCE` / `MUSUBI_ZONE` / `MUSUBI_DELIVERY_MODE`
environment variables, or a `config.json` in the plugin data directory:

```json
{
  "actor": "alice",
  "presence": "alice/laptop",
  "zone": "home",
  "delivery_mode": "shadow"
}
```

Identity keys must be all-or-nothing. `MUSUBI_HARNESS_BIN` and
`MUSUBI_MEMORY_DATA_BIN` may be set in either env or `config.json` to
override the PATH lookup for `musubi-harness` and `memory-data`.

## Prompt recall

Before each answer, the prompt is sent to Musubi as a search query and only the
memories that are really about it are added, each with its object id, plane,
state and date, labelled as historical, untrusted data. Relevance is gated on
the similarity score, with the floor set above the measured noise ceiling
rather than tuned to the hits. Each memory is shown once per session, and again
after a `/compact`.

**Privacy.** The prompt text leaves the machine as the query, which is more
than shadow-mode capture sends. So with the default `auto`, it runs only when
Delivery mode is `verified`. A prompt that looks like it contains a credential
is never sent. The hook has a hard deadline and never blocks the prompt; its
log records decisions and object ids, never prompt text.

## Live thoughts from other agents

In `verified` mode with a Musubi URL and token, list the presences you want to
hear from in **Live thought sources** (`/config`, e.g. `yua/laptop,tama/desk`).
A plugin monitor then streams their thoughts to you live, and each arrives in
the session as a notification Claude can react to without being asked:

```
musubi thought from yua/laptop [3Jsg…] (untrusted data): the deploy is green, your turn
```

Thoughts are another agent's words: they are labelled untrusted and are never
instructions. The sender shown is the owner of the stream the thought arrived
on, not the name written in the thought, so one agent cannot pose as another
(a mismatched claim is shown as `yua/laptop (claims to be …)`). Every field is
shape-checked and stripped of control characters, so a thought cannot forge a
second line. Monitors run in interactive sessions only.

**Disclosure: a bearer token at rest.** Monitor processes receive no plugin
settings, so the SessionStart hook writes the URL, token, your presence and the
watched namespaces to `<plugin data>/monitor/stream.json`, or, for a seat whose
launcher sets `MUSUBI_ACTOR`, to its own `stream.<actor>__<seat>.json` built from
that seat's environment, never from the shared settings (a 0700 directory,
file created 0600, the same boundary as the local outbox). SessionEnd deletes
it, but a crash can leave it behind until the next session rewrites it. It is
written only in `verified` mode with sources set, and removed otherwise.

## Failure is visible, never silent

Any capture, config or delivery failure is recorded as a structured reason in
the plugin's `degraded.jsonl`, and the hook still exits 0, so capture degrades
visibly and never breaks your session. The file holds failure codes only,
never the conversation that caused them. `/musubi-claude:health` summarises it.

## Development

```bash
git clone https://github.com/sourceblender/musubi-claude
cd musubi-claude
python -m venv .venv
source .venv/bin/activate
pip install -e .
pip install ruff mypy pytest pytest-cov
ruff check scripts tests
ruff format --check scripts tests
mypy scripts
pytest
```

CI is `ruff` + `mypy --strict` + `pytest` on Python 3.12.

## License

Apache-2.0.
