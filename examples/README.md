# NovaFabric examples

Runnable examples, each answering a different "why would I use
NovaFabric?" question. Most are regression tests under
`tests/test_example_*.py`, so the docs cannot drift from working code.

## The examples

| Example | Use it to see... | External deps |
|---|---|---|
| [`minimal-agent-run/`](minimal-agent-run/) | NovaFabric capturing a real Anthropic LLM round-trip into a single capsule | `anthropic`, `ANTHROPIC_API_KEY` |
| [`replay-and-diff/`](replay-and-diff/) | Why replay and diff matter — capture two runs of the same script with a controlled change, then have NovaFabric tell you what differs | none (pure stdlib) |
| [`lineage-chain/`](lineage-chain/) | What the lineage graph answers — "if this dataset changes, which runs depend on it?" | none (pure stdlib) |
| [`spkg-anomaly-scan/`](spkg-anomaly-scan/) | The Security & Provenance KG flagging a suspicious lineage edge with a MITRE ATT&CK label (`nova kg detect`, experimental) | none (pure stdlib) |
| [`eval-and-intervention/`](eval-and-intervention/) | The zero-token eval loop (`nova eval offline`, `nova diff --significance`) and counterfactual intervention replay (`nova replay --mode intervention`, experimental) | none (pure stdlib) |
| [`prompt-and-analytics/`](prompt-and-analytics/) | Why manage prompts as versioned, labeled assets and analyze runs offline — `nova prompt`/`label`, variant-tagged captures, then `nova query`/`view`/`trend`/`session`/`diff --group-by variant` (experimental) | none (pure stdlib) |
| [`azure-openai/`](azure-openai/) | NovaFabric working against non-default OpenAI endpoints (Azure, on-prem, gateways) | `openai`, Azure deployment |
| [`langchain-agent/`](langchain-agent/) | Capturing a real LangChain (+ LangGraph) tool-using agent without code changes | `langgraph` + `langchain-anthropic` *or* `langchain-openai` + key |
| [`blackbox_demo/`](blackbox_demo/) | End-to-end "black box for agents" walkthrough (capture → seal → verify → replay) | none (pure stdlib) |
| [`hpc-slurm-job/`](hpc-slurm-job/) | The intended way to capture a **Slurm batch job** — and what the capsule does not record about it (no job id, node, or cluster) | Slurm to submit; none to run locally |
| [`docker-run/`](docker-run/) | What a capsule of a **containerized** run actually contains — and what it does not (the env lock describes the host, and no image digest is recorded) | Docker daemon |
| [`notebook-capture/`](notebook-capture/) | The two working ways to capture **Jupyter notebook** work — and the four things a notebook capsule does not contain (cell output among them) | `nbconvert` + `ipykernel`, in the same env as NovaFabric |
| [`plugin-hook-reference/`](plugin-hook-reference/) | The wire-level hook plugin contract — a minimal third-party capture plugin | none (pure stdlib) |

Support directories (not runnable examples): `assets/` (sample asset
specs), `capsules/` (sample captured capsules), `reports/` (sample
report output).

The stdlib-only examples run in any clean checkout. The provider-backed
ones need keys and SDKs but **all indexed examples exit cleanly with a
skip message** when prerequisites are missing — safe in CI and on
first-time clones.

## Quick sanity sweep (no keys needed)

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
multi-vendor strategy RFC
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

See the [contributing guide](../CONTRIBUTING.md) and the
[tutorials](../docs/tutorials/README.md) for
context.
