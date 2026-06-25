# How NovaFabric capture works

> **Who this is for:** engineers who want to understand what happens under the hood
> when you run `nova capture` — what is being intercepted, how, and why it works
> without changing your agent code.

---

## Start with HTTP

Every LLM call your agent makes is an HTTP request. HTTP is the same protocol your
browser uses. When you visit a website, the browser sends a request and gets a
response back. Python agents do exactly the same thing — they just don't open a
browser window.

When your `hpc-analyst` agent calls Ollama:

```
agent code  →  HTTP POST http://localhost:11434/api/chat  →  Ollama
                         { model: "qwen3:35b", messages: [...] }

Ollama      →  HTTP 200 response  →  agent code
                         { message: { content: "OOM failure caused by..." } }
```

NovaFabric sits in the middle of this exchange and records both sides.

---

## What `requests` and `aiohttp` are

They are Python libraries for making HTTP calls. Most Python code uses one of them.

**`requests`** — the simple, synchronous one:

```python
import requests

response = requests.post(
    "http://localhost:11434/api/chat",
    json={"model": "qwen3:35b", "messages": [{"role": "user", "content": "hello"}]}
)
print(response.json())
```

**Synchronous** means: your program stops at that line and waits until the server
responds. Nothing else happens. Like a phone call — you stay on the line until the
other person answers.

**`aiohttp`** — the async one:

```python
import aiohttp, asyncio

async def call():
    async with aiohttp.ClientSession() as session:
        async with session.post("http://localhost:11434/api/chat", json={...}) as r:
            return await r.json()

asyncio.run(call())
```

**Asynchronous** means: your program sends the request, then goes and does other
things while waiting. Like sending an email — you don't freeze; you go do other
work and check back when it arrives.

LangGraph uses `aiohttp` because it runs many things concurrently — tool calls,
multiple agents, streaming — and can't afford to freeze at each one.

| | `requests` | `aiohttp` |
|---|---|---|
| Style | synchronous (`def`) | async (`async def`) |
| Good for | simple scripts, one call at a time | agents, servers, concurrent work |
| Used by | older SDKs, simple tools | LangGraph, FastAPI, OpenAI SDK v1+ |

Both do the same job. NovaFabric captures both.

---

## How NovaFabric intercepts them — monkey-patching

When `nova capture python agent.py` runs, it starts the agent as a child process.
Before the agent code loads, NovaFabric injects a small loader via `PYTHONPATH`.
That loader **replaces** the standard HTTP functions with its own versions.

Here is the concept, simplified:

```python
import requests

# save the original function
_real_post = requests.Session.send

def _nova_send(self, request, **kwargs):
    # 1. record what is being sent
    nova_record_request(request)

    # 2. call the real function — nothing changes for the agent
    response = _real_post(self, request, **kwargs)

    # 3. record what came back
    nova_record_response(response)

    # 4. return normally — agent sees nothing different
    return response

# swap in nova's version
requests.Session.send = _nova_send
```

The agent calls `requests.post(...)` as usual. It gets back a normal response.
It has no idea its call was intercepted. Everything is captured automatically.

The same pattern applies to `aiohttp.ClientSession._request` for async code, and
to `urllib3` (the lowest-level library that both `requests` and `boto3` use
internally).

---

## The URL registry — what gets captured

Not every HTTP call should be captured. When your agent downloads a file, queries
a database, or calls an internal API, you don't want that mixed in with LLM calls.

NovaFabric uses a URL registry (`capture/hooks/url_registry.yaml`) to classify
HTTP calls:

```yaml
# url_registry.yaml
- pattern: "localhost:11434"    # Ollama
  provider: ollama
  capture: full

- pattern: "api.openai.com"     # OpenAI
  provider: openai
  capture: full

- pattern: "bedrock.amazonaws.com"  # AWS Bedrock
  provider: bedrock
  capture: full
```

Only URLs that match a known LLM provider pattern are captured. Everything else
passes through unrecorded. You can add your own entries to
`~/.novafabric/url_registry.yaml` for private LLM endpoints or internal gateways.

---

## Why this approach — not an SDK

The alternative to wire-level capture is SDK instrumentation: you import a library
and wrap every function you want to trace. Langfuse works this way.

The problem with SDK instrumentation at scale:

- Every framework (LangGraph, AutoGen, CrewAI, custom code) needs its own integration
- If you miss one call site, it isn't captured
- Third-party agents you can't modify can't be captured at all
- Every SDK update can break the instrumentation

Wire-level capture at the HTTP layer solves all of these:

- Every Python agent, regardless of framework, eventually calls Ollama / OpenAI /
  Bedrock through `requests` or `aiohttp`
- There is only one place to hook, not hundreds
- Third-party agents work without modification
- SDK updates don't break capture — the HTTP call still happens

The tradeoff: you see the HTTP conversation, not the internal agent reasoning steps.
You see "what was sent to the model" and "what came back," but not "which LangGraph
node decided to make this call." For most capture purposes, the HTTP conversation is
the right level of detail.

---

## What goes into the capsule

Every intercepted LLM call produces one record in `events.jsonl`:

```json
{
  "gen_ai.system": "ollama",
  "gen_ai.request.model": "qwen3:35b",
  "gen_ai.request.messages": [
    {"role": "system", "content": "You are an HPC analyst..."},
    {"role": "user",   "content": "Analyze this SLURM error log..."}
  ],
  "gen_ai.response.choices": [
    {"message": {"role": "assistant", "content": "This is an OOM failure..."}}
  ],
  "gen_ai.usage.input_tokens": 312,
  "gen_ai.usage.output_tokens": 187,
  "duration_ms": 4821,
  "timestamp": "2026-05-10T19:50:44.905Z"
}
```

These fields follow the OpenTelemetry GenAI semantic conventions — the same
standard used by Prometheus, Jaeger, and other observability tools. Capsules are
not a proprietary format.

---

## The four capture modes

| Mode | How it works | When to use |
|---|---|---|
| `nova capture python agent.py` | Subprocess wrap + hook injection | You control process startup |
| `nova api-proxy --port 9900` | HTTP proxy in front of the LLM | Agent is a service, or non-Python |
| `nova mcp-proxy -- python mcp_server.py` | Proxy in front of the MCP server | Agent uses MCP tools |
| `install_hooks()` in-process | Direct hook installation | Notebooks, embedded agents |

All four produce the same capsule format. All four use the same URL registry.
