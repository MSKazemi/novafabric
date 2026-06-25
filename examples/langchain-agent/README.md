# langchain-agent

**Use this when you want to:** see NovaFabric capturing a real
LangChain (+ LangGraph) tool-using agent end-to-end, with no code
changes to the agent itself.

This is the dogfooding example for anyone whose daily AI work runs on
LangChain / LangGraph / deepagents.

## What it proves

Calling `nova capture` around any LangChain or LangGraph agent
captures every LLM round-trip into `model-calls.jsonl` — including the
tool-call requests the model makes and the messages flowing back —
**without modifying the agent**. The capture works because LangChain
provider integrations (`langchain-openai`, `langchain-anthropic`) call
the underlying SDKs that NovaFabric already hooks.

## What this example transitively covers

| Framework | Why this example covers it |
|---|---|
| LangChain LCEL chains | Same provider integrations under the hood |
| LangGraph state graphs | This example uses one (`create_react_agent`) |
| deepagents | Wraps `create_react_agent` — same transports |

So the recommendation is: **use this one example to verify capture
works for any LangChain-family agent.** A separate `langgraph-agent` or
`deepagents-agent` example would be a near-duplicate maintenance burden.

## Run it

```bash
# Either provider works — pick whichever you have a key for.
export ANTHROPIC_API_KEY=sk-...
uv pip install langgraph langchain-anthropic
# OR:
# export OPENAI_API_KEY=sk-...
# uv pip install langgraph langchain-openai

uv run nova capture --output-dir examples/langchain-agent/runs \
    python examples/langchain-agent/agent.py
```

## Inspect the capsule

```bash
RUN=examples/langchain-agent/runs/<run-id>

# Validate
nova validate $RUN

# Read each LLM round-trip
cat $RUN/model-calls.jsonl | jq -c '{
  call: .gen_ai.request.messages[-1].content,
  response: .gen_ai.response.choices[0].message
}'

# Replay forensically
nova replay $RUN --mode forensic
```

## Honest limitations

- **Local Python tool calls are not separately recorded** in
  `tool-calls.jsonl`. NovaFabric only logs MCP tools and explicit-protocol
  tools as discrete tool-call records. The LangChain tool the model
  invokes (here, `add_two_numbers`) executes in-process; its execution
  doesn't go over the wire, so there's no transport for the tool hook
  to intercept.
- **However, the chain is not lost** — the tool-call *request* appears
  in the LLM response (`.tool_calls`), and the tool *result* appears in
  the next call's prompt messages. You can reconstruct what happened
  from the captured stream.
- For separate tool-call capture, route tools through MCP (see
  [`docs/concepts.md`](../../docs/concepts.md) on the MCP hook) or
  write a [hook plugin](../../docs/integrations/writing-a-hook-plugin.md).

## Skip behavior

The script exits cleanly with exit 0 if neither
`ANTHROPIC_API_KEY` + `langchain-anthropic` nor
`OPENAI_API_KEY` + `langchain-openai` is available.
Safe in CI without credentials.
