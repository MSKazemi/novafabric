# smolagents capture (experimental, since v0.109.0)

Wrap a [smolagents](https://github.com/huggingface/smolagents) agent and every `run()` writes one
Run Capsule: a span per step and a record per tool call. This example needs **no network, no
Hugging Face token and no model** — a scripted model answers a toy stock question in two steps.

```bash
pip install novafabric smolagents       # smolagents is not a NovaFabric extra
python examples/smolagents-capture/agent.py            # prints the capsule path
python examples/smolagents-capture/agent.py ./runs     # or choose where capsules go
```

It exits `0` with a skip message when `smolagents` is not installed.

What to look at in the printed capsule directory:

- `trace.jsonl` — one `smolagents.step.*` span per step, plus the run span.
- `tool-calls.jsonl` — one record per tool call. Arguments and observations are **digested, not
  stored**, unless `NOVA_CAPTURE_LEVEL=forensic`.
- `capsule.yaml` — the manifest. This adapter writes **no** model calls; those come from the wire
  hooks when a real model is used (see the [Hugging Face guide](../../docs/integrations/huggingface.md)).

The adapter is observe-only: the agent's answer is returned unchanged, and replay never serves these
tool records.
