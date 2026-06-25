"""LangChain (+ LangGraph) example: a tool-using agent, captured end-to-end.

NovaFabric captures LangChain calls **without code changes**, because
LangChain's provider integrations (`langchain-anthropic`,
`langchain-openai`) ultimately call the underlying SDKs that NovaFabric
already hooks. Same hook → same `model-calls.jsonl` schema → same
replay/diff/lineage primitives.

This example uses `langgraph.prebuilt.create_react_agent`, which is the
modern recommended way to build a LangChain tool-using agent. By
covering this path, the example *transitively* covers:

- **LangChain LCEL chains** (same provider integrations underneath)
- **LangGraph state graphs** (this example uses one — `create_react_agent`
  returns a graph)
- **deepagents** (wraps `create_react_agent` with presets — same wire)

What gets captured:
  ✓ Every LLM round-trip (request messages, response, tool-call requests
    the model makes, token usage) into `model-calls.jsonl` via the
    openai/anthropic SDK hooks.
  ✗ Local Python tool execution is NOT separately recorded into
    `tool-calls.jsonl` — only MCP and explicit-protocol tools are.
    However, the tool's *result* shows up in the next LLM call's prompt,
    so the full chain is reconstructable from the captured stream.

Run:
    export ANTHROPIC_API_KEY=sk-...     # or OPENAI_API_KEY=sk-...
    uv pip install langgraph langchain-anthropic   # or langchain-openai
    uv run nova capture --output-dir examples/langchain-agent/runs \\
        python examples/langchain-agent/agent.py

Skips cleanly if neither key is set or required packages are missing.
"""
from __future__ import annotations

import os
import sys


def _select_model() -> tuple[str, str] | None:
    """Pick (provider, model_id) based on which key is available.

    Tries Anthropic first (the user's primary), falls back to OpenAI.
    Returns None if neither is usable.
    """
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            import langchain_anthropic  # noqa: F401
            return ("anthropic", "claude-haiku-4-5-20251001")
        except ImportError:
            pass
    if os.environ.get("OPENAI_API_KEY"):
        try:
            import langchain_openai  # noqa: F401
            return ("openai", "gpt-4o-mini")
        except ImportError:
            pass
    return None


def _build_chat_model(provider: str, model_id: str):  # type: ignore[no-untyped-def]
    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(model_name=model_id, max_tokens=512, timeout=30, stop=None)
    from langchain_openai import ChatOpenAI
    return ChatOpenAI(model=model_id, max_tokens=512, timeout=30)


# A trivially-deterministic local tool. Real agents call APIs here.
def add_two_numbers(a: int, b: int) -> int:
    """Add two integers and return the sum."""
    return a + b


def main() -> int:
    pick = _select_model()
    if pick is None:
        print("[langchain-agent] No usable provider key + LangChain integration found.")
        print("[langchain-agent]   Need: ANTHROPIC_API_KEY + langchain-anthropic, OR")
        print("[langchain-agent]         OPENAI_API_KEY + langchain-openai.")
        print("[langchain-agent] Skipping cleanly — exit 0.")
        return 0

    try:
        from langchain_core.tools import tool
        from langgraph.prebuilt import create_react_agent
    except ImportError as e:
        print(f"[langchain-agent] LangGraph not installed: {e}")
        print("[langchain-agent]   uv pip install langgraph")
        print("[langchain-agent] Skipping cleanly — exit 0.")
        return 0

    provider, model_id = pick
    print(f"[langchain-agent] Using provider={provider} model={model_id}")

    llm = _build_chat_model(provider, model_id)
    add_tool = tool(add_two_numbers)
    agent = create_react_agent(llm, tools=[add_tool])

    result = agent.invoke({
        "messages": [
            ("user", "What is 17 plus 25? Use the add_two_numbers tool. "
                     "Then answer in one short sentence.")
        ],
    })

    final_msg = result["messages"][-1].content
    print(f"\nAgent final answer: {final_msg}")
    print("\nInspect the capsule:")
    print("  ls examples/langchain-agent/runs/")
    print("  cat <capsule>/model-calls.jsonl | head -1 | jq .")
    print("  # Look for the tool-call request in the model's response, and")
    print("  # the tool result in the next call's prompt — that's the chain.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
