"""Deterministic dummy 'agent' for the replay-and-diff example.

No LLM, no network, no external dependencies. The script's behavior is
controlled by AGENT_MODE so we can capture two runs that differ in a
known way, then use `nova diff` to surface the change.

Run two captures with different modes, then diff them. See README.md.
"""
from __future__ import annotations

import json
import os
import sys


def main() -> int:
    mode = os.environ.get("AGENT_MODE", "baseline")
    if mode == "baseline":
        result = {"summary": "ok", "items_processed": 100, "score": 0.85}
    elif mode == "regressed":
        # A subtle behavior regression a real diff should catch.
        result = {"summary": "ok", "items_processed": 100, "score": 0.62}
    else:
        print(f"unknown AGENT_MODE={mode!r}", file=sys.stderr)
        return 2

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
