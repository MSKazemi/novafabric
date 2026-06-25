# minimal-agent-run

**Use this when you want to:** see NovaFabric capture a real LLM round-trip
end-to-end, into a single capsule directory you can validate and inspect.

## What it does

1. Decorates a Python function with `@agent(name=..., capsule_dir=...)`.
2. Inside the function, calls Anthropic's API (one short message).
3. NovaFabric's capture hooks intercept the SDK call and record it into
   `capsule/model-calls.jsonl` along with `trace.jsonl`, `env.lock`,
   `redaction-proof.json`, and `capsule.yaml`.

## Run it

```bash
export ANTHROPIC_API_KEY=sk-...
uv pip install anthropic    # not a NovaFabric dep — install on demand
uv run python examples/minimal-agent-run/agent.py
```

## Inspect the capsule

```bash
nova validate examples/minimal-agent-run/capsule
cat examples/minimal-agent-run/capsule/model-calls.jsonl  # one line per LLM call
cat examples/minimal-agent-run/capsule/capsule.yaml
```

## Without an API key

The script exits cleanly with a message if `ANTHROPIC_API_KEY` is unset or
if the `anthropic` SDK is not installed. This keeps the example safe to
run in CI and on first-time clones.
