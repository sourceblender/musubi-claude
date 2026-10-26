"""Put ``scripts/`` on ``sys.path``, the way the plugin's own launcher does.

``musubi-claude-run`` runs every hook and the MCP server with ``scripts/`` on the
path, so the modules in it import each other bare (``import musubi_claude_seat_env``).
``scripts/`` is an implicit namespace package, so without this the tests — which
import ``scripts.musubi_claude_runtime`` — would resolve the outer module and then
fail on its sibling import. Several tests already insert this directory by hand
for exactly this reason; doing it once here means production code needs one import
spelling instead of a try/except pair, and mypy sees each file under one name.
"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
