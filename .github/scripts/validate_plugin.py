#!/usr/bin/env python3
"""Strict plugin validation, minus one known advisory.

`claude plugin validate --strict` fails on every warning. Since Claude Code
2.1.29x it warns that the name "musubi-claude" reads as one of Anthropic's own;
renaming a published plugin breaks existing installs, so that is a separate
decision. Until it is made, this gate keeps strict's meaning for everything
else: any error fails, and any warning other than that exact name advisory
fails.
"""

import json
import subprocess
import sys

# The exact advisory, as the validator reports it, at the exact paths it uses.
NAME_ADVISORY = 'Plugin name "musubi-claude" reads as one of Anthropic\'s own. Name it for what it does.'
KNOWN_ADVISORIES = {
    ("marketplace", "plugins[0].name", NAME_ADVISORY),
    (
        "plugin",
        "name",
        NAME_ADVISORY + " If this is one of Anthropic's own plugins, validate the marketplace that lists it.",
    ),
}


def main() -> int:
    run = subprocess.run(
        ["claude", "plugin", "validate", "--json", "."],
        capture_output=True,
        text=True,
    )
    try:
        report = json.loads(run.stdout)
    except json.JSONDecodeError:
        print(run.stdout, run.stderr, sep="\n")
        print("validate did not return a JSON report", file=sys.stderr)
        return 1
    if report.get("success") is not True:
        print(f"validator reported success={report.get('success')!r}", file=sys.stderr)
        return 1
    manifests = [report.get("manifest"), *report.get("contents", [])]
    for m in manifests:
        if not isinstance(m, dict) or not m.get("file") or m.get("type") not in ("plugin", "marketplace"):
            print(f"validator returned a manifest it did not examine: {m!r}", file=sys.stderr)
            return 1
    failures = []
    for m in manifests:
        for e in m.get("errors", []):
            failures.append(f"error {m['file']} {e.get('path')}: {e.get('message')}")
        for w in m.get("warnings", []):
            if (m["type"], w.get("path"), w.get("message")) in KNOWN_ADVISORIES:
                print(f"known advisory, allowed: {m['file']} {w.get('path')}")
                continue
            failures.append(f"warning {m['file']} {w.get('path')}: {w.get('message')}")
    print(f"validated {len(manifests)} manifest(s); exit {run.returncode}")
    for f in failures:
        print(f, file=sys.stderr)
    return 1 if failures or run.returncode != 0 else 0


if __name__ == "__main__":
    sys.exit(main())
