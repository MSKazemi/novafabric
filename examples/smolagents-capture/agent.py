"""Credential-free smolagents capture example (experimental, since v0.109.0).

No network, no Hugging Face token, no model: a scripted model answers a toy stock question in two
steps. It shows what ``wrap_agent`` writes -- one Run Capsule, a span per step, a record per tool
call -- and that the agent's own answer is returned unchanged.

    pip install novafabric smolagents       # smolagents is not a NovaFabric extra
    python examples/smolagents-capture/agent.py [OUTPUT_DIR]

Exits 0 with a skip message when ``smolagents`` is not installed.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path


def _jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def main() -> int:
    try:
        from smolagents import Tool, ToolCallingAgent, models
    except ImportError:
        print("smolagents is not installed (pip install smolagents). Skipping cleanly.")
        return 0

    from novafabric.adapters.smolagents import wrap_agent

    class Lookup(Tool):
        name = "lookup"
        description = "Look up the stock level of a SKU."
        inputs = {"sku": {"type": "string", "description": "the SKU"}}
        output_type = "string"

        def forward(self, sku: str) -> str:
            return f"{sku}: 0 on hand"  # invented data

    def reply(name: str, arguments: dict) -> "models.ChatMessage":
        call = models.ChatMessageToolCall(
            id=f"call-{name}",
            type="function",
            function=models.ChatMessageToolCallFunction(name=name, arguments=arguments),
        )
        return models.ChatMessage(role="assistant", content="", tool_calls=[call])

    class ScriptedModel(models.Model):
        def __init__(self) -> None:
            super().__init__(model_id="scripted")
            self._queue = [
                reply("lookup", {"sku": "SKU-1042"}),
                reply("final_answer", {"answer": "SKU-1042 is out of stock."}),
            ]

        def generate(self, messages, stop_sequences=None, response_format=None,
                     tools_to_call_from=None, **kwargs):
            return self._queue.pop(0)

    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="smolagents-"))
    agent = wrap_agent(
        ToolCallingAgent(tools=[Lookup()], model=ScriptedModel(), verbosity_level=-1),
        run_name="stock-bot",
        data_dir=out,
    )
    print("answer :", agent.run("Is SKU-1042 in stock?"))

    (capsule,) = [p.parent for p in out.glob("*/capsule.yaml")]
    spans = [s for s in _jsonl(capsule / "trace.jsonl") if s["name"].startswith("smolagents.step")]
    steps = [s for s in spans if s["attributes"]["step_type"] != "final_answer"]  # closing marker
    tools = _jsonl(capsule / "tool-calls.jsonl")
    print("capsule:", capsule)
    print("steps  :", [s["attributes"]["step_type"] for s in steps])
    print("tools  :", [t["tool_name"] for t in tools], "(arguments are digested, not stored)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
