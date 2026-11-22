# Hugging Face

**Status: experimental.** Implemented and tested offline against the real `smolagents` 1.26.0 and
`huggingface_hub` 2.3.0 libraries, but **not yet in a release** — the entry points below exist on
`main` only. Every feature on this page is labelled with exactly one of *works today*,
*experimental*, *planned* or *future design*; where a label is not stated, it is **experimental**.

NovaFabric is an independent open-source project. It is not affiliated with, certified by or endorsed
by Hugging Face.

NovaFabric records and verifies evidence about runs that *use* Hugging Face models, datasets, inference
and `smolagents`. It does not train, serve or orchestrate anything, and it does not decide whether a
model is safe or was lawfully trained. None of this needs a Hugging Face account, and none of it is
required: a run that never touches these entry points is captured exactly as before.

## What is recorded, and what is not

| Surface | Recorded | Replay | Status |
|---|---|---|---|
| `smolagents` agent run (`wrap_agent`) | one capsule per `run()`; a span per step; one tool-call record per tool call named in a step (name, argument digest, step outcome) | **not intercepted** — tools run live | experimental |
| `InferenceClient.chat_completion` through the Inference Providers router (`https://router.huggingface.co/…`, auto-routed or an explicit provider) | the request, requested model and selector, status, timing; since ADR-0324 the response body (generic wire) | **served** from the recorded bytes at the `httpx` transport (ADR-0324); a capsule recorded before is refused by a strict replay (ADR-0319) | experimental |
| the same with `stream=True`, and `AsyncInferenceClient` | the request at send time; the stream is copied as your code reads it, never read ahead | served as the recorded chunks (as above) | experimental |
| Dedicated Inference Endpoint (`https://*.endpoints.huggingface.cloud/…`) | as above, with system `huggingface.endpoint` | served (as above) | experimental |
| A custom `base_url` | **not captured** unless you add it through a URL-registry override | — | — |
| Hub metadata, dataset and weight downloads | **never** recorded | — | — |
| Hub model, dataset or space revision references | an opt-in receipt you create yourself (below) | — | experimental |
| Evaluation receipts and `.eval_results` export | an opt-in receipt over an existing experiment (below) | — | experimental |

Since ADR-0324 (**experimental**) a Hugging Face model call is recorded with its response body as
generic wire bytes and mocked replay serves it from those bytes at the `httpx` transport, like any
registry model host (see the [replay support matrix](../architecture/replay-modes.md)); this is not yet
tested end to end through the real `InferenceClient`. A capsule recorded before ADR-0324 holds no
response, so a strict mocked replay **refuses** it (`ModelSurfaceNotServable`, ADR-0319);
`--permissive` runs it live and reports it. `smolagents` tools are *capture only*: a replay runs them
live.

## smolagents

```bash
pip install novafabric smolagents    # smolagents is not a NovaFabric extra
```

```python
from novafabric.adapters.smolagents import wrap_agent   # or: from novafabric.adapters import wrap_smolagents

agent = wrap_agent(agent, run_name="support-bot")
answer = agent.run("Is SKU-1042 in stock?")
```

`wrap_agent` patches `run` in place and returns the same object (wrapping twice is a no-op). `run`
keeps its arguments, return value (including `RunResult`), exceptions and the laziness of
`stream=True`. Without `smolagents` installed the module still imports and `wrap_agent` raises
`ImportError` naming the install command. The run name defaults to the agent's `name`, then its class.

Each `run()` writes one Run Capsule to `data_dir`, or to `$NOVAFABRIC_HOME/runs/<run-id>/` /
`./.novafabric/runs/<run-id>/` when none is given (the same defaults as the other framework adapters,
see [CLI reference § Framework Adapters](../cli-reference.md)). It holds:

- **A span per step** in `trace.jsonl` (`smolagents.step.action`, `.planning`, `.final_answer`): step
  number, timing, error *class* (never the message; a run-level failure likewise keeps only the class unless `NOVA_CAPTURE_LEVEL=forensic`), token counts when `smolagents` reports them, the
  number of tool calls, and a digest of the step's observations.
- **A record per tool call** in `tool-calls.jsonl`, at *step* granularity: `smolagents` reports the
  calls a step made and one combined outcome for the step, not a result per call, so an error is
  attributed to every call in that step (`outcome_scope: step`). A tool that raised leaves no completed
  call in the framework; the record then carries what the model *asked for* and is marked
  `requested_only`. The framework's final-answer call is recorded like any other.
- **The run's end status**: `success`, `failure` (the exception still propagates), or `partial` — a
  stream abandoned, the task cancelled, or `agent.interrupt()` honoured (recorded as *cancelled*, not
  failed). A run that stops at `max_steps` returns a final answer, so its capsule still says `success`
  but carries the tag `agent_state=max_steps_error`.

Model calls are **not** written by the adapter. The wire-level hooks record them (see below), so a
call is never counted twice.

Managed (sub-)agents in `agent.managed_agents` are wrapped recursively and appear as
`smolagents.managed_agent.run` spans **inside the parent's capsule**, not as capsules of their own,
including when `smolagents` runs them from a worker thread.

### What this adapter cannot see

- **Tools called from generated code.** A `CodeAgent` step is recorded as one `python_interpreter`
  call. The tools that the generated code calls are not individually visible, and the generated code
  itself is not stored.
- **Per-call tool results.** `result` is always `null`; only the step-level outcome is recorded.
- **Replay.** Tool calls are observed, never served: the records are marked not servable and carry a
  surface marker that replay does not intercept, so a mocked replay does not stand in for any
  `smolagents` tool. Re-running an agent from its capsule is **future design**.
- **Payloads.** The adapter's own records (`trace.jsonl`, `tool-calls.jsonl`) keep digests, not
  arguments, observations, model output or final answers, unless `NOVA_CAPTURE_LEVEL=forensic` (tool
  arguments are then kept; the generated code and observations still are not). This is the adapter's
  records only — see [Privacy](#privacy-what-a-capsule-holds-by-default) for model calls.

### Try it without a model or a token

The script below needs no network, no Hugging Face token and no model: a scripted model answers a toy
stock question in two steps. It was run against `smolagents` 1.26.0 and produced one capsule with three
spans under the run span (two action steps and the final-answer marker) and two tool-call records
(`lookup`, `final_answer`).

```python
import tempfile
from pathlib import Path

from smolagents import Tool, ToolCallingAgent, models

from novafabric.adapters.smolagents import wrap_agent


class Lookup(Tool):
    name = "lookup"
    description = "Look up the stock level of a SKU."
    inputs = {"sku": {"type": "string", "description": "the SKU"}}
    output_type = "string"

    def forward(self, sku: str) -> str:
        return f"{sku}: 0 on hand"  # invented data


def _reply(name: str, arguments: dict) -> models.ChatMessage:
    call = models.ChatMessageToolCall(
        id=f"call-{name}", type="function",
        function=models.ChatMessageToolCallFunction(name=name, arguments=arguments),
    )
    return models.ChatMessage(role="assistant", content="", tool_calls=[call])


class ScriptedModel(models.Model):
    def __init__(self) -> None:
        super().__init__(model_id="scripted")
        self._queue = [
            _reply("lookup", {"sku": "SKU-1042"}),
            _reply("final_answer", {"answer": "SKU-1042 is out of stock."}),
        ]

    def generate(self, messages, stop_sequences=None, response_format=None,
                 tools_to_call_from=None, **kwargs):
        return self._queue.pop(0)


out = Path(tempfile.mkdtemp(prefix="smolagents-capture-"))
agent = wrap_agent(
    ToolCallingAgent(tools=[Lookup()], model=ScriptedModel(), verbosity_level=-1),
    run_name="stock-bot",
    data_dir=out,
)
print(agent.run("Is SKU-1042 in stock?"))
print("capsule:", next(out.iterdir()))
```

Read `trace.jsonl` and `tool-calls.jsonl` in the printed directory. The capsule is unsealed, as every
default capture is (sealing is opt-in); `nova verify <capsule>` says so rather than passing it.

## Inference capture (InferenceClient, router and Endpoints)

Nothing to wrap: when NovaFabric's wire hooks are active (`nova capture`, or an adapter capsule), a
request to either of two HTTPS hosts is recorded as a model call:

- `https://router.huggingface.co/…` — the Inference Providers router, for `provider="auto"` and for an
  explicit provider alike (`/together/…`, `/groq/…`, `/hf-inference/…`, …); `gen_ai.system` is
  `huggingface`.
- `https://*.endpoints.huggingface.cloud/…` — dedicated Inference Endpoints; `gen_ai.system` is
  `huggingface.endpoint`.

These two registry entries use `match_kind: regex`, which is anchored at the start of the URL: it pins
the scheme and the whole host. A URL that merely *contains* one of these in its path, query or fragment,
a host that continues the name (`router.huggingface.co.example`), and plain `http://` are not recorded.
The match is deliberately **not** a bare `huggingface.co`: the Hub itself (metadata, datasets, weight
files) lives there and is not a model call. `match_kind` is an optional registry field; every existing
entry stays a substring match.

The identity fields are parsed from the **request only**. Since ADR-0324 the wire hook also stores the
response body (`io.novafabric.wire.response`, copied as your code reads it, never read ahead), but no
Hugging Face decoder parses it. A Hugging Face model-call record therefore carries an
`io.novafabric.model_identity` block that says exactly what is and is not known:

| Field | Value | Meaning |
|---|---|---|
| `requested_model` | the `model` you sent, minus a routing selector (router only) | observed |
| `requested_selector` / `selector_kind` | `fastest`, `cheapest`, `preferred` (a policy) or a known provider name (an explicit provider); any other `:tag` such as `:latest` stays part of the model; none for a dedicated endpoint | what you *asked for* — not what happened |
| `reported_model` | `null` | **not parsed** — the raw response body is stored, but not decoded |
| `served_provider` | `null` | **not parsed** — the router selects a provider server-side, and nothing decodes the response |
| `identity_source` | `request_only` | |

`gen_ai.response.model` on every wire record mirrors the request; for Hugging Face records read this
block instead. Token counts are not recorded either. A Hub commit is a repository revision, not proof of
which weights a provider served.

Limits: every classified route, including embeddings, is labelled `operation: chat`; the
Responses API (beta) has no route-specific handling. Replay serves these records from the recorded
bytes (generic wire, ADR-0324, **experimental**).

## Privacy: what a capsule holds by default

This is a project-wide default, not something specific to Hugging Face. Wire-level model-call capture
(`model-calls.jsonl`) stores request messages — **prompt text** — at every capture level, for every
provider including Hugging Face; `NOVA_CAPTURE_LEVEL` does not change that
([CLI reference § nova policy capture-level](../cli-reference.md)). Since ADR-0324 the response body —
**completion text** — is stored too, at every capture level, exactly as OpenAI responses are;
`NOVAFABRIC_CAPTURE_WIRE_RESPONSES=off` stores no response bodies (the calls are then refused at
replay). If prompts or completions are sensitive, do not share the capsule unreviewed.

The `Authorization` header is not written to a capsule. The secret scanner includes a rule for Hugging
Face tokens, so a token pasted into a prompt is redacted at finalisation — scanning is rule-based and
is not proof that no secret remains.

## Hub revision receipts

*Experimental. Python API only* — there is no `nova` command for this, and capture does not create
receipts for you. You declare a reference, optionally resolve it, and attach the result to a manifest
yourself.

```python
from novafabric.interop.hf_hub import (
    attach_hub_references, declare_reference, resolve_reference,
)

ref = declare_reference("dataset", "owner/name", revision="main", config="default", split="test")
ref = resolve_reference(ref)          # the ONLY call here that touches the network
manifest = attach_hub_references(manifest, [ref])   # returns a copy under extensions["dev.novafabric.huggingface-hub"]
```

`resolve_reference` is anonymous by default (`token=False` is passed to the Hub client, so an ambient
`HF_TOKEN` is not sent; pass `token=` for a private or gated repo) and needs the optional
`huggingface_hub` package. It never raises for a Hub condition: the outcome and a reason from a closed
vocabulary (`offline`, `not_found`, `unauthorized_or_gated`, `rate_limited`, `hub_unreachable`, …) are
recorded in the receipt, and `HF_HUB_OFFLINE` makes it return `unavailable/offline` without importing
the client. No text supplied by the Hub or by an exception is copied into the receipt.

Each receipt carries one of three strengths, with the claim it is allowed to make stamped beside it:

| Strength | Meaning |
|---|---|
| `declared_only` | You stated it. Nothing was checked. |
| `hub_resolved` | The Hub returned a commit for the revision at `resolved_at`. A branch or tag can move later; only a revision you gave as a full 40-hex commit is `pinned`. |
| `local_bytes_verified` | Files you hold hash to the SHA-256 the Hub published (Git LFS) for that revision; covers only those files (`verify_local_file`). A digest you supply yourself is recorded as `declared` and can never raise the strength. |

`check_revision_drift(previous, current)` answers `same`, `moved` or `unknown` for two resolutions of
the same reference. Offline `nova verify` never needs the Hub, and a capsule without the extension is
valid and unchanged.

## Evaluation receipts and `.eval_results`

*Experimental. Python API only.* `build_eval_receipt` binds an **existing, finalized** experiment (the
one the eval subsystem already produced) to a pinned dataset reference, and optionally a model
reference. Every number in the receipt is the experiment's own — counts and values are copied, the 95%
Wilson interval is computed from them — and the receipt lists what it does **not** establish, such as a
moving dataset revision, items that produced no score, a small sample, an unpinned harness, or a model
declared rather than resolved.

```python
from novafabric.eval.experiment import load_experiment
from novafabric.eval.hf_receipt import build_eval_receipt, to_eval_results, write_eval_results

experiment = load_experiment("<experiment-id>")            # must be finalized
receipt = build_eval_receipt(experiment, dataset_ref)      # dataset_ref: a resolved "dataset" reference
entries = to_eval_results(receipt, metric="exact_match", task_id="qa_test", on="2026-10-28")
write_eval_results(entries, "out/.eval_results/acme-run.yaml")
```

A sample of what `to_eval_results` produced against a small offline experiment (the dataset id and
commit are synthetic):

```yaml
- dataset:
    id: acme/qa
    task_id: qa_test
    revision: cccccccccccccccccccccccccccccccccccccccc
  value: 1.0
  notes: Recorded by NovaFabric, not a Hugging Face verified score. exact_match pass_rate
    (fraction in [0, 1]), n=3 of 3 items. 95% Wilson interval [0.439, 1.000]. experiment
    sha256:a1cc960dbcdf344efc585a2a0eb6d2dfeeae3b6d2c52dab00008a316f79e315b
  date: '2026-10-28'
```

- Exporting is opt-in and writes a **local** file, only inside a folder named `.eval_results`, and
  refuses to overwrite unless asked. Nothing is uploaded; putting the file on a model repo is a
  separate step you take yourself.
- **`verifyToken` is never written** — that badge belongs to Hugging Face's own jobs — and the
  validator refuses it. The Hub's `.eval_results` format is a work in progress
  (<https://huggingface.co/docs/hub/eval-results>, checked 2026-10-28) and defines no scale for
  `value`, so the scale is stated in `notes`. The shape NovaFabric validates may need to change when the
  format settles.
- You supply `task_id`; it comes from the benchmark's own `eval.yaml`, which NovaFabric cannot know.
- `render_card_backlink` and `append_card_backlink` produce an evidence section for a model or dataset
  card as plain text, replacing only NovaFabric's own marked block and changing no other byte.
- `compare_receipts` restates an existing two-experiment comparison next to both receipts' limitations.
  A difference is not attributed to a model revision unless both model references are resolved, and even
  then provider routing, sampling and prompts can change outputs.

A receipt is evidence recorded by NovaFabric. It is not a Hugging Face verified score, and it does not
show that the benchmark is uncontaminated.

## Known limits

- **Replay:** Hugging Face model calls are served from recorded bytes since ADR-0324 (experimental; a
  capsule recorded before is refused by a strict replay, ADR-0319); `smolagents` tools are never served.
- **`CodeAgent`:** tools called from generated code are not individually visible.
- **Identity:** the reported model and served provider of an Inference Providers call are recorded as
  unknown (`null`), not guessed.
- **Prompts** are stored by default for every provider, including Hugging Face (above); the Hugging Face
  token itself is not written.
- **Coverage:** a custom `base_url` is not captured without a registry override; Hub downloads are never
  recorded.
- **`wrap_agent` and copies:** wrap an agent *after* any `copy.copy` / `copy.deepcopy`; a copy of an
  already-wrapped agent still calls the original agent's `run`.
- **Managed agents:** wrap only the top-level agent. A managed sub-agent that was also wrapped on its own
  opens a second capsule when the parent runs it from a worker thread.
- **Streams:** a `run(stream=True)` generator that is closed before it ever started finalises its
  capsule only when it is garbage collected.
- **Secret shapes:** the scanner recognises `hf_…` user tokens of the usual length; an `api_org_…` token
  is not a recognised shape, and the query string of an endpoint URL is kept in the request record.
- **Hub receipts** are validated for internal consistency, not authenticity: anyone can write a
  well-formed receipt, so a receipt is evidence only together with the capsule it travels in.
- **Hub formats:** `.eval_results` is a work-in-progress Hub format.
- **Release:** none of this is in a published release yet. The `huggingface_hub` and `smolagents`
  packages are optional and are not NovaFabric extras.
