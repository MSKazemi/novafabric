# How NovaFabric capture works

> **Who this is for:** engineers who want to understand what happens under the hood
> when you run `nova capture` — what is being intercepted, how, and why it works
> without changing a single line of your agent code.

**What you will learn**

- Why every LLM call is really an HTTP request, and where NovaFabric sits in that exchange.
- The difference between the `requests` and `aiohttp` libraries, and why NovaFabric hooks both.
- How wire-level **monkey-patching** captures calls with zero code changes.
- How the **URL registry** decides which HTTP calls become part of the capsule.
- Why NovaFabric captures at the HTTP layer instead of instrumenting each SDK.
- What a captured LLM call looks like as a `model-calls.jsonl` record, in
  [OpenTelemetry GenAI semantic conventions](https://opentelemetry.io/docs/specs/semconv/gen-ai/).
- The four capture modes and when to reach for each.

Capture is the mechanism behind the **Run Capsule** — the second of NovaFabric's five
primitives (Asset Registry, Run Capsule, Replay, Lineage, Evidence Bundle). Everything
here produces the same portable, schema-valid, secret-redacted capsule you can later
replay, diff, and export as an Evidence Bundle.

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

NovaFabric sits in the middle of this exchange and records both sides. It never
answers the request itself and never sits in the request path as a dependency — if
capture failed entirely, the agent's HTTP call would still complete. That is a
deliberate invariant: **capture must never block the workload.**

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

Both do the same job. NovaFabric captures both — and, because it also hooks the
lower-level libraries described below, it captures calls that never touch either of
these two by name.

---

## How NovaFabric intercepts them — monkey-patching

When `nova capture python agent.py` runs, it starts the agent as a child process.
Before the agent code loads, NovaFabric injects a small loader — a generated
`sitecustomize.py` placed on `PYTHONPATH`, which Python imports automatically at
interpreter startup. That loader **replaces** the standard HTTP functions with its
own versions. No import statement, decorator, or config change is needed in the
agent itself.

Here is the concept, simplified:

```python
import requests

# save the original function
_real_send = requests.Session.send

def _nova_send(self, request, **kwargs):
    # 1. record what is being sent
    nova_record_request(request)

    # 2. call the real function — nothing changes for the agent
    response = _real_send(self, request, **kwargs)

    # 3. record what came back
    nova_record_response(response)

    # 4. return normally — agent sees nothing different
    return response

# swap in nova's version
requests.Session.send = _nova_send
```

The agent calls `requests.post(...)` as usual. It gets back a normal response.
It has no idea its call was intercepted. Everything is captured automatically, and
the patches are removed once the run finishes.

The same pattern applies across the wire-level surface NovaFabric ships hooks for:

| Library | What it covers |
|---|---|
| `requests` | The most common synchronous HTTP client |
| `aiohttp` | The async client used by concurrent agents |
| `urllib3` | The lowest-level library that both `requests` and `boto3` use internally |
| `httpx` | The client under the OpenAI and Anthropic SDKs v1+ |
| Bedrock | AWS Bedrock runtime calls |

Alongside these wire-level hooks, NovaFabric also installs per-SDK hooks
(OpenAI `Completions.create`, Anthropic `Messages.create`) and an MCP hook
(`ClientSession.call_tool`) so that tool calls are captured too. Third-party plugins
are auto-discovered through the `novafabric.hooks` entry-point group, so an
agent-framework author can add a hook without patching NovaFabric. If an SDK isn't
present, its hook is silently skipped — a capsule is still written even when no AI
SDK is installed at all.

---

## The URL registry — what gets captured

Not every HTTP call should be captured. When your agent downloads a file, queries
a database, or calls an internal API, you don't want that mixed in with LLM calls.

NovaFabric uses a vendored URL registry (`capture/hooks/url_registry.yaml`) to
classify HTTP calls by provider:

```yaml
# url_registry.yaml
schema_version: "0.1.0"
patterns:
  - match: "localhost:11434"        # Ollama
    gen_ai_system: ollama
    transport: http

  - match: "api.openai.com"         # OpenAI
    gen_ai_system: openai
    transport: http

  - match: "bedrock-runtime."       # AWS Bedrock (all regions)
    gen_ai_system: aws.bedrock
    transport: http
```

Only URLs that match a known LLM provider pattern are recorded into
`model-calls.jsonl`. Everything else passes through unrecorded. The vendored
registry ships classifiers for providers including OpenAI, Anthropic, Cohere,
Together, Mistral, Replicate, Bedrock, and Ollama.

You can add your own entries by dropping a `~/.novafabric/url_registry.yaml` file,
which the loader prefers over the vendored default (or point `$NOVAFABRIC_URL_REGISTRY`
at an absolute path, which takes precedence over both) — useful for private LLM
endpoints, mirrors, or internal gateways. Nothing calls out to a hardcoded external
service; every endpoint is configurable to a private one.

---

## Why this approach — not an SDK

The alternative to wire-level capture is SDK instrumentation: you import a library
and wrap every function you want to trace. Observability tools such as Langfuse work
this way.

The problem with SDK instrumentation at scale:

- Every framework (LangGraph, AutoGen, CrewAI, custom code) needs its own integration.
- If you miss one call site, it isn't captured.
- Third-party agents you can't modify can't be captured at all.
- Every SDK update can break the instrumentation.

Wire-level capture at the HTTP layer solves all of these:

- Every Python agent, regardless of framework, eventually calls Ollama / OpenAI /
  Bedrock through `requests`, `aiohttp`, `httpx`, or `urllib3`.
- There is only one place to hook, not hundreds.
- Third-party agents work without modification.
- SDK updates don't break capture — the HTTP call still happens.

**The tradeoff:** you see the HTTP conversation, not the internal agent reasoning
steps. You see "what was sent to the model" and "what came back," but not "which
LangGraph node decided to make this call." For capture, replay, and audit purposes,
the HTTP conversation is the load-bearing level of detail — it is exactly what you
need to replay a call from cache, diff two runs, or prove what a model was asked.

---

## What goes into the capsule

A Run Capsule is a directory identified by a [ULID](https://github.com/ulid/spec)
(a time-sortable ID). Each intercepted LLM call produces one line in
`model-calls.jsonl`, recorded in the
[OpenTelemetry GenAI semantic conventions](https://opentelemetry.io/docs/specs/semconv/gen-ai/):

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
  "finished_at": "2026-05-10T19:50:44.905Z"
}
```

These fields follow the `gen_ai.*` OpenTelemetry GenAI semantic conventions — an open
standard, readable by the wider OpenTelemetry ecosystem (Jaeger and other OTel
tooling), not a proprietary NovaFabric format. That is deliberate: the capsule, not a
row in a hosted database, is the unit you own, tar, archive, and read air-gapped.

`model-calls.jsonl` is one file among several the capsule holds — it also carries
`capsule.yaml` (the manifest), `trace.jsonl` (OTel spans), `tool-calls.jsonl`,
`assets.jsonl`, `env.lock` (Python version, installed packages, safe env vars,
OS/arch/CPU/mem, GPU presence), `redaction-proof.json`, `replay.yaml`,
`lineage.jsonl`, and `inputs/` + `outputs/`. Determinism-relevant request
parameters — including `gen_ai.request.temperature`, `gen_ai.request.top_p`, and
`gen_ai.request.seed` — are recorded so that `exact` replay can be judged eligible.

Before the capsule is finalized, a secret scanner redacts matches in place
(`[REDACTED:rule-id]`) and writes `redaction-proof.json`. A capsule without that
proof is invalid to `nova validate` and cannot be exported as an Evidence Bundle —
so secrets never leave in a shared capsule. The capsule is written on **both success
and failure**; a failed run sets `status: failure` and records an `error` block
rather than producing nothing.

---

## Extended event streams

Beyond model and tool calls, a capsule can carry the extended event streams of
the ADR-0082 taxonomy: `network_events.jsonl`, `human_approvals.jsonl`,
`file_events.jsonl`, `state_transitions.jsonl`, `memory_operations.jsonl`,
`guardrail_events.jsonl`, `evaluator_events.jsonl`, `reranker_events.jsonl`,
and `vector_retrievals.jsonl`. Until v0.63 only network events and human
approvals had default-path producers — the other seven were recorder-side
APIs with no public entry point. ADR-0209 changed that. What fills each
stream today, honestly labeled:

| Stream | Filled by | Status |
|---|---|---|
| `network_events.jsonl` | wire-level hooks — `requests`, `httpx`, and (since ADR-0209) `aiohttp` + `urllib3`, layering-guarded against double-recording | works today |
| `human_approvals.jsonl` | `nova seal propose` maker-checker flow | works today |
| `guardrail_events.jsonl` | OpenAI Agents adapter: SDK guardrail spans map to events automatically; or your own `record.guardrail(...)` calls | experimental |
| `state_transitions.jsonl` | LangGraph adapter: one digest-chained event per `stream()` node update, a start→end pair per `invoke()`; or `record.state_transition(...)` | experimental |
| `vector_retrievals.jsonl` | `record.wrap_retriever(...)` around any retriever callable (explicit opt-in, no auto-detection); or `record.vector_retrieval(...)` | experimental |
| `file_events.jsonl` | your `record.file_event(...)` calls only — **no file-I/O auto-capture exists** | façade only; auto-capture is future design |
| `memory_operations.jsonl` | your `record.memory_operation(...)` calls (feeds the ADR-0143 memory-provenance lineage) | façade only |
| `evaluator_events.jsonl` / `reranker_events.jsonl` | your `record.evaluator(...)` / `record.reranker(...)` calls | façade only; adapter wirings are future design |

The façade is `novafabric.capture.record` (see the
[Python API reference](../python-api.md#extended-event-recording-experimental)):
outside a capture run every call is a silent no-op, so instrumented code runs
unchanged in production. Two policies apply on the default path: NovaFabric's
own wirings record **digests, counts, names, scores, and timings** at the
default capture level and attach raw payloads (state dicts, document text)
only at `forensic`/`air_gapped` (`NOVA_CAPTURE_LEVEL`); and all nine streams
are covered by the finalize-time secret scanner, so free text in guardrail
details or evaluator rationales is redacted with proof like everything else.
There is **no heuristic auto-detection** — an event stream NovaFabric cannot
honestly observe stays façade-only rather than being guessed at.

---

## The four capture modes

All four modes below produce the same capsule format and share the same URL
registry. Pick by how you launch and control the agent:

| Mode | How it works | When to use |
|---|---|---|
| `nova capture python agent.py` | Subprocess wrap + `sitecustomize.py` hook injection | You control process startup |
| `nova api-proxy --port 9900` | Transparent HTTP proxy in front of the LLM | Agent is a service, or a non-Python client |
| `nova mcp-proxy -- python mcp_server.py` | Transparent stdio proxy in front of the MCP server | Agent uses MCP tools |
| `@novafabric.agent` decorator | Direct hook installation from inside your process | Notebooks, embedded agents |

The `@novafabric.agent` decorator wraps a function in-process without spawning a
subprocess; under the hood it calls `install_all(writer, parent_span_id)`
(`capture/hooks/__init__.py`), the same installer the subprocess path uses.

---

## Summary

- LLM calls are HTTP requests; NovaFabric records both sides without answering them
  or blocking the workload.
- It captures by **monkey-patching** wire-level HTTP libraries (`requests`,
  `aiohttp`, `urllib3`, `httpx`, Bedrock) plus per-SDK and MCP hooks, injected via a
  generated `sitecustomize.py` on `PYTHONPATH` — zero agent code changes.
- A **URL registry** classifies which calls become `model-calls.jsonl` records; you
  can override it at `~/.novafabric/url_registry.yaml`.
- Records use open OpenTelemetry GenAI semantic conventions, inside a portable Run
  Capsule you own — written on success and failure, with verifiable redaction.

## Next steps

- **Run it:** capture a real command and inspect the capsule, then validate it with
  `nova validate`.
- **Replay it:** re-execute the capsule with external calls controlled — see the
  Replay primitive's four modes (`forensic`, `mocked`, `semantic`, `exact`).
- **Compare runs:** use `nova diff` (with `--assert-no-regressions` as a CI gate) to
  see exactly what changed between two capsules.
- **Prove it:** export a signed Evidence Bundle with `nova export-evidence` — an
  ed25519-signed ZIP verifiable offline with only `sha256sum` plus an ed25519
  verifier, no NovaFabric runtime required.
