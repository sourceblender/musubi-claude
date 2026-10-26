---
name: health
description: Report whether Musubi memory is actually working in this session - settings, identity, transport, capture and delivery queues, recent degradations, and any stranded records - in plain words.
disable-model-invocation: true
argument-hint: "[--probe]"
allowed-tools: Bash(musubi-claude-health *)
---

# Musubi memory health

Local state, read-only (no conversation content, no credentials):

```!
musubi-claude-health --plugin-data "${CLAUDE_PLUGIN_DATA}" $ARGUMENTS
```

Explain the report above to the user in a short, plain summary. Rules:

- Lead with one verdict line: **working**, **working with problems**, or
  **not working**, and why.
- `setup: false` → not set up; tell them to run `/musubi-claude:setup`.
- `error: plugin_data_unknown` → the report could not find the plugin's data
  directory, so it says nothing about setup. Do not suggest setup; quote the
  `fix`.
- `identity.ok: false` → quote the error code; point to `/config` →
  musubi-claude (**Musubi actor**, **Seat**, **Zone**).
- Delivery: `delivery_mode: shadow` means nothing has left this machine, by
  design. With `verified`, give the counts: verified vs still pending vs dead.
  **Pending is not stored** — never call pending records remembered.
- `other_root.undelivered` > 0 → say plainly that that many records sit in an
  older state directory and will not be delivered from there; this needs a
  deliberate move, not a retry.
- Degradations: name the top one or two reasons and the latest time. Say they
  are capture skips (a turn that was not recorded), not data loss of stored
  memories.
- `provider` (only with `--probe`): reachable or not. Unreachable is not the
  same as empty memory.
- Do not paste the raw JSON unless the user asks. Do not invent numbers that
  are not in the report.
