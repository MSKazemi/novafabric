"""Deterministic dummy tool-using 'agent' for the eval + intervention tutorials.

No LLM, no network, no third-party dependencies — stdlib only. The agent runs
three deterministic "tool calls" (two word-counts and one lookup) and, when
running under ``nova capture``, self-reports each call into the capsule's
``tool-calls.jsonl`` via the ``NOVAFABRIC_CAPSULE_DIR`` env var (the same
cooperation contract ``examples/blackbox_demo`` uses for ``outputs/``).

In a real deployment tool calls are recorded for you by the MCP hook
(`ClientSession.call_tool`), `nova mcp-proxy`, or a capture-hook plugin — the
self-report here only keeps this example runnable offline with zero
third-party dependencies.

AGENT_MODE=baseline   → consistent behavior (metamorphic invariant holds)
AGENT_MODE=regressed  → the whitespace-variant input gets a different answer
                        (a consistency regression `nova eval offline` catches)
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def word_count(text: str, mode: str) -> int:
    count = len(text.split())
    # The planted regression: the whitespace-variant input is miscounted.
    if mode == "regressed" and text != text.strip():
        count += 1
    return count


def lookup(key: str) -> str:
    table = {"capital:france": "Paris", "capital:italy": "Rome"}
    return table[key]


def main() -> int:
    mode = os.environ.get("AGENT_MODE", "baseline")
    if mode not in ("baseline", "regressed"):
        print(f"unknown AGENT_MODE={mode!r}", file=sys.stderr)
        return 2

    calls = [
        ("word_count", "Hello World"),
        ("word_count", "  hello   world "),
        ("lookup", "capital:france"),
    ]

    records = []
    for i, (tool_name, tool_input) in enumerate(calls):
        started = _now_iso()
        t0 = time.monotonic()
        if tool_name == "word_count":
            value: object = word_count(tool_input, mode)
        else:
            value = lookup(tool_input)
        records.append(
            {
                "tool_call_id": f"tool-{i:04d}",
                "tool_name": tool_name,
                "input": tool_input,
                "output": {"value": value},
                "status": "success",
                "started": started,
                "finished": _now_iso(),
                "duration_ms": int((time.monotonic() - t0) * 1000),
            }
        )

    # Self-report into the live capsule (no-op outside `nova capture`).
    capsule_dir = os.environ.get("NOVAFABRIC_CAPSULE_DIR")
    if capsule_dir:
        with (Path(capsule_dir) / "tool-calls.jsonl").open("a") as f:
            for rec in records:
                f.write(json.dumps(rec) + "\n")

    print(json.dumps({"mode": mode, "tool_calls": len(records)}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
