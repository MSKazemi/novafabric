# NovaFabric examples

Five runnable examples, each answering a different "why would I use
NovaFabric?" question. Each one is a regression test under
`tests/test_example_*.py`, so the docs cannot drift from working code.

## The five

| Example | Use it to see... | External deps |
|---|---|---|
| [`minimal-agent-run/`](minimal-agent-run/) | NovaFabric capturing a real Anthropic LLM round-trip into a single capsule | `anthropic`, `ANTHROPIC_API_KEY` |
| [`replay-and-diff/`](replay-and-diff/) | Why replay and diff matter — capture two runs of the same script with a controlled change, then have NovaFabric tell you what differs | none (pure stdlib) |
| [`lineage-chain/`](lineage-chain/) | What the lineage graph answers — "if this dataset changes, which runs depend on it?" | none (pure stdlib) |
| [`spkg-anomaly-scan/`](spkg-anomaly-scan/) | The Security & Provenance KG flagging a suspicious lineage edge with a MITRE ATT&CK label (`nova kg detect`, experimental) | none (pure stdlib) |
| [`azure-openai/`](azure-openai/) | NovaFabric working against non-default OpenAI endpoints (Azure, on-prem, gateways) | `openai`, Azure deployment |
| [`langchain-agent/`](langchain-agent/) | Capturing a real LangChain (+ LangGraph) tool-using agent without code changes | `langgraph` + `langchain-anthropic` *or* `langchain-openai` + key |

The first three need no external dependencies and run in any clean
checkout. The last two need provider keys and SDKs but **all five exit
cleanly with a skip message** when prerequisites are missing — safe in
CI and on first-time clones.

## Quick all-three sanity sweep (no keys needed)

```bash
# replay-and-diff
AGENT_MODE=baseline uv run nova capture --output-dir examples/replay-and-diff/runs python examples/replay-and-diff/agent.py
AGENT_MODE=regressed uv run nova capture --output-dir examples/replay-and-diff/runs python examples/replay-and-diff/agent.py
ls examples/replay-and-diff/runs/

# lineage-chain
for s in train-v1 eval-v1 promote-v1; do
  uv run nova capture --output-dir examples/lineage-chain/runs python examples/lineage-chain/step.py $s
done
nova lineage blast-radius local:datasets/training-set@1.0.0
```

## Why aren't there separate LangGraph / deepagents examples?

Because **`langchain-agent/` covers them transitively**. LangChain, LangGraph,
and deepagents all bottom out at the same provider SDKs
(`langchain-openai`, `langchain-anthropic`) and HTTP transports
(`httpx`, `requests`) that NovaFabric already hooks. One LangChain
example proves the capture works for the entire family. Adding
LangGraph and deepagents examples would be near-duplicate maintenance
surface for the same proof.

If you find a real capture *gap* specific to LangGraph or deepagents
(some path that doesn't go through the shared transports), open an
issue — that's a fix-first item in core, not a new example to bandage
around.

## Why aren't there OpenAI Agents SDK / litellm / Vercel AI SDK examples?

For the same reason: they all wire-level capture identically because
they use the same HTTP transports. The
[multi-vendor strategy RFC](../design/governance/RFC-0001-multi-vendor-strategy.md)
made wire-level the primary growth axis for exactly this reason —
one hook covers many frameworks.

## Adding new examples

Examples are deliberately bounded. Before adding one, check:

1. **Does it answer a *new* "why" question** — or is it a feature demo
   of an existing one? Feature demos rot.
2. **Is the framework you want to demo not already covered transitively**
   by an existing example via shared transports?

If yes to (1) and (2), add it with:

- A `README.md` opening with **"Use this when you want to..."**.
- A regression test at `tests/test_example_<name>.py` that runs the
  example end-to-end and asserts user-visible behavior.
- **Clean skip behavior** when external resources (keys, network, heavy
  optional deps) are unavailable.

See [CLAUDE.md](../CLAUDE.md) anti-patterns and the
[tutorials](../docs/tutorials/README.md) for
context.
