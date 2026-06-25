# azure-openai

**Use this when you want to:** prove NovaFabric works against
non-default OpenAI endpoints — Azure deployments, on-prem proxies,
or any provider that uses the openai SDK with a custom `base_url`.

## What it proves

NovaFabric's openai-SDK capture hook fires on **any** call routed
through the `openai.AzureOpenAI` (or `openai.OpenAI` with custom
`base_url`) client. The capture is endpoint-agnostic — the same
`model-calls.jsonl` schema lands regardless of whether the call hit
`api.openai.com`, an Azure deployment, or an internal LLM gateway.

## Run it

```bash
export AZURE_OPENAI_ENDPOINT=https://<your-resource>.openai.azure.com
export AZURE_OPENAI_API_KEY=<your-azure-key>
export AZURE_OPENAI_DEPLOYMENT=<your-deployment-name>     # e.g. gpt-4o-mini
export OPENAI_API_VERSION=2024-10-21                      # optional

uv pip install openai
uv run nova capture --output-dir examples/azure-openai/runs \
    python examples/azure-openai/agent.py
```

## Inspect

```bash
nova validate examples/azure-openai/runs/<run-id>
cat examples/azure-openai/runs/<run-id>/model-calls.jsonl | head -1 | jq .
```

You should see `gen_ai.system: "openai"` (the SDK identifier, not the
back-end provider) and `gen_ai.request.model` set to your deployment
name. The capsule is otherwise identical to a public-OpenAI capture.

## Without keys

The script exits cleanly with a skip message if any of the three
env vars are unset or the `openai` SDK is not installed. Safe in CI.
