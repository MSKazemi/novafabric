"""Minimal NovaFabric example: capture a real Anthropic LLM round-trip.

Uses the @agent decorator. NovaFabric auto-installs capture hooks for the
Anthropic SDK before the function runs and removes them after, so every
LLM call inside `run_agent()` is recorded into `model-calls.jsonl`.

Run:
    export ANTHROPIC_API_KEY=sk-...
    uv run python examples/minimal-agent-run/agent.py

Then inspect the capsule:
    nova validate examples/minimal-agent-run/capsule
    cat examples/minimal-agent-run/capsule/model-calls.jsonl

If ANTHROPIC_API_KEY is unset or the `anthropic` SDK is not installed,
the script exits cleanly with a helpful message — it does not fail loudly,
because the example doubles as a regression-tested fixture and we do not
want CI to require API keys.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from novafabric.sdk.agent import agent

CAPSULE_DIR = Path(__file__).parent / "capsule"


def _have_prereqs() -> tuple[bool, str]:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return False, "ANTHROPIC_API_KEY not set — skipping live LLM call."
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False, "`anthropic` SDK not installed — run `uv pip install anthropic`."
    return True, ""


@agent(name="minimal-agent", version="0.1.0", capsule_dir=CAPSULE_DIR)
def run_agent() -> str:
    import anthropic

    client = anthropic.Anthropic()
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[
            {"role": "user", "content": "In one sentence: what is replayable AI infrastructure?"}
        ],
    )
    text = "".join(block.text for block in response.content if block.type == "text")
    print(f"Model said: {text}")
    return text


def main() -> int:
    ok, why = _have_prereqs()
    if not ok:
        print(f"[minimal-agent-run] {why}")
        print("[minimal-agent-run] Skipping cleanly — exit 0.")
        return 0

    result = run_agent()
    print(f"\nAgent returned: {result!r}")
    print(f"Capsule written to: {CAPSULE_DIR}")
    print("\nInspect with:")
    print(f"  nova validate {CAPSULE_DIR}")
    print(f"  cat {CAPSULE_DIR}/model-calls.jsonl")
    return 0


if __name__ == "__main__":
    sys.exit(main())
