# Replay modes

[Architecture, as built](README.md) › Replay modes

`nova replay <capsule|run-id> --mode <mode>` (`cli/replay.py:replay_cmd`) loads a
capsule and passes it to `replay/_engine.py:ReplayEngine.run`. There are five
modes. Two of them, `mocked` and `intervention`, run the captured command again.
The other three read the capsule and execute nothing.

![The five replay modes](../assets/architecture/replay-modes.svg)

```mermaid
flowchart LR
    C[(Capsule)] --> E{ReplayEngine.run}
    E -->|always| CK["env check (_env_check.py)<br/>tool-schema drift (ADR-0128)"]
    E -->|mocked · default| M["subprocess re-run<br/>MockModelDispatcher + MockToolDispatcher<br/>serve recorded responses · fail closed"]
    E -->|forensic| F["read-only report"]
    E -->|semantic| S["similarity of recorded<br/>responses (difflib)"]
    E -->|exact| X["byte-exact eligibility check"]
    E -->|intervention · experimental| I["substitute one event,<br/>re-run under mocked semantics"]
    M & F & S & X & I --> R["replays/&lt;ulid&gt;/replay_result.yaml"]
    I --> CC[(counterfactual capsule<br/>replay_mode: intervention)]
```

## What every mode does first

Before it branches, `ReplayEngine.run`:

- loads `capsule.yaml`, `replay.yaml`, `env.lock`, `model-calls.jsonl` and
  `tool-calls.jsonl`;
- compares the recorded environment with the current one
  (`replay/_env_check.py:EnvironmentResolver`) and records any differences as
  `env_warnings`;
- re-validates each recorded tool call against its **current** declared schema
  (`capture/schema_validation.py:revalidate_tool_calls`, ADR-0128) and records
  any drift. Only `exact` mode refuses on drift.
- if `--allow-mutating` is passed, evaluates the `replay_mutating` policy and
  writes the decision to the audit log.

Every mode writes `replay_result.yaml` (`replay/_result.py:write_replay_result`,
`schemas/replay-result.schema.json`) under `.novafabric/replays/<ulid>/` in the
current directory, or under `-o <dir>`.

## The modes

| Mode | Executes the command? | What it does | Maturity |
|---|---|---|---|
| `mocked` (default) | **Yes**, in a subprocess, with a 600 s timeout | Re-runs `capsule.yaml:command`. A `sitecustomize.py` installs `replay/_dispatcher.py:MockModelDispatcher` (recorded responses for the supported model surfaces — sync or async, streamed or not — one queue per API surface) and `MockToolDispatcher` (recorded MCP `ClientSession.call_tool` results, matched one-to-one). **Fail-closed** on divergence; `--permissive` only reports. Tools on other surfaces run live; outbound connections are reported, not blocked. See [the mocked-replay contract](#the-mocked-replay-contract-adr-0300-adr-0304) and the [support matrix](#support-matrix). | works today (Python workloads, supported surfaces only) |
| `forensic` | No | Read-only inspection. Reports call counts, environment warnings and schema drift. | works today |
| `semantic` | No | Scores how similar the recorded model responses within the capsule are to one another: the mean pairwise `difflib.SequenceMatcher` ratio, from 0.0 to 1.0. This is a **text** similarity, not a judgment of meaning, and no live model is called. | works today |
| `exact` | No | An **eligibility check** for byte-exact replay. It requires `env.lock` mode `deterministic` and a `gen_ai.request.seed` on every model call, and refuses if there is any tool-schema drift. Reports `exact_eligible` and `exact_reasons`. | works today |
| `intervention` | **Yes**, under mocked semantics | Needs `--intervention-file spec.yaml`. Substitutes one recorded model or tool event as the `InterventionSpec` describes (`replay/_intervention.py`), re-runs everything downstream with zero live model calls, and writes a minimal counterfactual capsule (`capsule.yaml` with `replay_mode: intervention` and `replay_of_run_id`, plus the call streams), so you can `nova diff` it against the original. | experimental (ADR-0086) |

## The mocked-replay contract (ADR-0300, ADR-0304)

`replay/_contract.py` is the one place that decides what the dispatchers may
serve and how a replay is summarised; the engine and the in-process dispatchers
both use it.

**What is served.** A recorded model call is servable when it belongs to a
served API surface — OpenAI Chat Completions, the OpenAI Responses API (records
marked `extensions["io.novafabric.api_surface"]: "openai.responses"`), or
Anthropic Messages — its `status` is `success`, and it has at least one recorded
choice. Each surface has its own queue, in recorded order; a sync, async or
`stream=True` call to a surface takes the next record from that queue, and a
streamed call gets the record back as the chunk or event stream the SDK would
have produced (ADR-0304). (Every SDK call is also recorded once more by the
`httpx` wire hook, with no choices; that duplicate is never served.) A recorded tool call is
servable when it is an MCP `tools/call` with a tool name; a call captured both by
the in-process MCP hook and by `nova mcp-proxy` counts once.

**How tool calls are matched** (`ToolCallMatcher`), one-to-one: by an exact
`tool_call_id` when the surface carries one (MCP `call_tool` does not); else by
tool name plus an order-insensitive hash of the arguments, earliest unconsumed
record first — so repeated identical calls get distinct recorded results in
recorded order. A record is never served twice. Anything else is **unmatched**.

**Divergence policy.** Under the default `fail` policy the replayed process
raises a named `ReplayDivergenceError` subclass (`replay/_errors.py`) at the call
that diverged, instead of fabricating a reply or reaching the network:

| Divergence kind | When |
|---|---|
| `model_queue_exhausted` | more calls to an API surface than were recorded (`provider`, `surface`, `call_index`, `recorded_queue_length` reported) |
| `provider_mismatch` | the same, while another surface's recordings are unconsumed (e.g. a Chat Completions recording, replayed through the Responses API) |
| `order_mismatch` | a call reaches a different API surface than the recording did at that position (`surface`, `expected_surface`) |
| `unsupported_surface` | `chat.completions.parse`, `responses.parse`, legacy completions, Anthropic `messages.stream()` and `beta.messages`, `with_raw_response` / `with_streaming_response` |
| `malformed_recorded_response` | a recorded tool-call entry has no `name` |
| `tool_call_unmatched` | an MCP `call_tool` with no unconsumed recorded result — **the live tool is not run** |
| `model_calls_unconsumed` / `tool_calls_unconsumed` | (after the run) recorded responses that were never requested |
| `dispatcher_install_failed`, `multiple_interpreters` | the dispatcher could not be installed (the process is stopped, exit 86, before the workload runs), or several Python processes each consumed the queue |

Any divergence makes the result `status: failure` with `error.type:
ReplayDivergence` and a `divergence_reason` — even when the workload caught the
exception and exited 0, because the engine reads the dispatchers' event log, not
just the exit code. `--permissive` (`ReplayFlags(permissive=True)`) switches to
the `warn` policy: an exhausted queue serves an empty reply with a warning (the
pre-ADR-0300 behaviour), unsupported surfaces and unmatched MCP calls run live,
and every divergence is still recorded. `intervention` always uses `warn` and
installs no tool dispatcher, because a counterfactual is expected to diverge.

**What the result reports** (`replay_result.yaml`, additive and optional):

| Field | Meaning |
|---|---|
| `model_calls_mocked` / `model_calls_available` | recorded responses actually served / servable |
| `model_calls_unmatched` | calls with no recorded answer (including refused unsupported surfaces) |
| `tool_calls_mocked` / `tool_calls_available` / `tool_calls_recorded` | MCP results served / MCP results servable / every recorded tool call |
| `tool_calls_live` / `tool_calls_unmatched` | MCP calls run live (`--permissive` only) / MCP calls with no recorded result |
| `queues_fully_consumed` | every servable recording was requested |
| `divergence_reason` | the first divergence, plus a count of the others |
| `replay_contract` | policy, intercepted surfaces, `dispatcher_installed`, `model_calls_live`, unconsumed counts, `tool_calls_not_interceptable`, the network observation below, and the divergence list |
| `replay_contract.network_connections_live` / `network_destinations` | IPv4/IPv6 connections the replayed Python process opened (`socket.connect`), with the distinct `host:port` destinations (first 20). **Observed, never blocked** (ADR-0304); `network_observed: false` means nothing was observed, not that nothing happened |

`nova replay` prints the served counts, the live network connections and the
divergence reason under the "Replay written" line.

## Support matrix

Generated from `replay/_support_matrix.py` by
`scripts/gen_replay_support_matrix.py`; `tests/replay/test_support_matrix_is_generated.py`
fails if this table drifts from the rows, if a row's "served"/"refused" claim
drifts from the methods the dispatcher actually patches, or if a cited test no
longer exists. Edit the rows, not this table.

"Refused" means strict mocked replay raises `ReplayUnsupportedSurfaceError`
instead of letting the call reach the network; with `--permissive` it runs live
and is counted in `replay_contract.model_calls_live`.

<!-- BEGIN GENERATED: replay-support-matrix (scripts/gen_replay_support_matrix.py) -->

| Provider / API surface | Capture | Mocked replay | Streaming | Async | Status | Evidence |
|---|---|---|---|---|---|---|
| OpenAI Chat Completions `create` | SDK hook: full response incl. `tool_calls`; a streamed response is folded into one record (`nova.streaming`) | **Served** | served: the record is replayed as chunks (usage chunk when requested) | served | works today | `test_tool_choice_round_trip_e2e.py::test_openai_tool_calls_round_trip_through_capture_and_mocked_replay`; `test_model_surface_coverage_e2e.py::test_s14_async_chat_completions_round_trip`; `test_model_surface_coverage_e2e.py::test_s15_streamed_chat_completions_round_trip`; `test_model_surface_coverage_e2e.py::test_s15_async_streamed_chat_round_trip` |
| OpenAI `chat.completions.stream()` helper | through `create(stream=True)` | **Served** — the SDK's own stream accumulator runs on the replayed chunks | served | same path, not tested | works today | `test_model_surface_coverage_e2e.py::test_s15_chat_stream_helper_round_trip` |
| OpenAI Responses API `create` | SDK hook: output text and `function_call` items (as `tool_calls`), marked `io.novafabric.api_surface: openai.responses`; streamed calls recorded from the terminal event | **Served** — rebuilt `Response`; reasoning and hosted-tool output items are not recorded, so not served | served: the record is replayed as the event sequence, incl. `responses.stream()` | served | works today | `test_model_surface_coverage_e2e.py::test_s16_responses_api_round_trip_with_function_call`; `test_model_surface_coverage_e2e.py::test_s16_streamed_responses_api_round_trip`; `test_mocked_replay_contract.py::test_s11_a_chat_recording_is_not_served_to_the_responses_api` |
| OpenAI `chat.completions.parse`, `responses.parse` | wire hook: request only | Refused | — | refused | unsupported | `test_mocked_replay_contract.py::test_s17_unsupported_model_surfaces_are_refused` |
| OpenAI legacy `completions.create` | wire hook: request only | Refused | refused | refused | unsupported | `test_mocked_replay_contract.py::test_s17_unsupported_model_surfaces_are_refused` |
| `with_raw_response` / `with_streaming_response` (any served surface) | recorded without a response | Refused — the caller expects an HTTP response wrapper, which replay cannot build | refused | refused | unsupported | `test_mocked_replay_contract.py::test_s17_unsupported_model_surfaces_are_refused`; `test_sdk_stream_capture.py::test_raw_response_calls_are_recorded_without_choices` |
| Anthropic Messages `create` | SDK hook: full response incl. `tool_use`; finish reason mapped to the schema enum, raw value kept; a streamed response is folded into one record | **Served** — raw `stop_reason` served back | served: the record is replayed as raw stream events | served | works today (tested against a stand-in `anthropic` package; the real SDK is not a dependency) | `test_tool_choice_round_trip_e2e.py::test_anthropic_tool_use_round_trips_through_capture_and_mocked_replay`; `test_model_surface_coverage_e2e.py::test_s14_async_anthropic_messages_round_trip`; `test_model_surface_coverage_e2e.py::test_s15_streamed_anthropic_messages_round_trip` |
| Anthropic `messages.stream()` helper | not recorded with a response (it bypasses `create`) | Refused | refused | refused | unsupported | `test_mocked_replay_contract.py::test_s17_unsupported_model_surfaces_are_refused` |
| Anthropic `beta.messages` | wire hook: request only | Refused | refused | refused | unsupported | code: `UNSUPPORTED_MODEL_SURFACES` (real SDK not installed in CI) |
| Non-Python clients via `nova api-proxy` | streaming: merged response, canonical `tool_calls`; non-streaming: request + id/model only | Not intercepted — replay patches Python SDKs | — | — | capture only | `tests/test_api_proxy.py` |
| Other providers' SDKs, raw HTTP to a model API | wire hook: request only | Not intercepted — runs **live**; its connections are reported (`network_connections_live`) | — | — | not controlled | `test_mocked_replay_contract.py::test_s13_live_network_from_an_uncontrolled_tool_is_reported_not_blocked` |
| MCP `ClientSession.call_tool` (in-process hook or `nova mcp-proxy`) | full result (proxy: verbatim JSON-RPC envelope) | **Served** — one-to-one; an unmatched call is refused | — | (async by nature) | works today | `test_mocked_replay_contract.py::test_s2_model_tool_model`; `test_mocked_replay_contract.py::test_s4_repeated_identical_calls_consume_distinct_records`; `test_mocked_replay_contract.py::test_s8_missing_tool_record_fails_closed_even_if_the_workload_swallows_it` |
| MCP session set-up (server start, `initialize`, `list_tools`) | not recorded as tool calls | Not intercepted — runs **live** | — | — | not controlled | e2e tests start a real in-memory MCP server during replay |
| HTTP, shell, filesystem, framework-native tools | network/file events, not tool records | Not intercepted — runs **live**; outbound connections are reported (`network_connections_live`), files and processes are not | — | — | not controlled | `test_mocked_replay_contract.py::test_s13_network_tool_refused_and_uncontrolled_transports_reported`; `test_mocked_replay_contract.py::test_s13_live_network_from_an_uncontrolled_tool_is_reported_not_blocked` |

<!-- END GENERATED: replay-support-matrix -->

### What replay does not do today

These limits are stated so you can rely on the parts that do work:

- **Only the surfaces above are controlled.** If a workload's other tools have
  side effects (files, HTTP, databases, shell), a mocked replay repeats them.
  Run replays in a sandbox or against test credentials. The `--allow-readonly`,
  `--allow-mutating`, `--allow-external-side-effects` and
  `--allow-unknown-mutation` flags are evaluated by
  `replay/_policy.py:PolicyEvaluator`. Their per-call decisions are shown by
  `--dry-run` (which marks non-intercepted tools `[LIVE]`), but they do not
  intercept calls inside a mocked subprocess.
- **Capsules captured before ADR-0304 hold no response for async, streamed or
  Responses API calls** (the hooks wrapped only the sync, non-streaming methods).
  Replaying such a workload fails — the queue runs out (`model_queue_exhausted`)
  or is left unconsumed — but a call before that point can be served a record
  that belonged to a later call. Re-capture to replay it faithfully.
- **Recorded model errors are not replayed.** A call that raised during capture
  has an error record, which is never served; the replayed call at that position
  receives the next recorded response, and the replay then diverges.
- **A streamed response is recorded when the stream ends.** One the workload
  abandons is recorded with what it delivered and flagged
  `extensions["io.novafabric.stream_complete"]: false`; two streams consumed
  interleaved are recorded in the order they ended, which can differ from the
  order they started.
- **Network is reported, not refused.** A replay that reaches a database or an
  HTTP API through a tool replay does not intercept succeeds; the result names
  the destinations. Connections made by C extensions that bypass Python's
  `socket` methods, and by non-Python child processes, are not seen.
- **Model requests are not compared.** Responses are served by position per
  API surface; a changed prompt with the same call count and order is not a
  divergence here. Use `nova diff` against a fresh capture.
- **MCP results recorded by the in-process hook are lossy** for non-text content
  (image `mimeType`, embedded resources, `structuredContent` are not recorded);
  `nova mcp-proxy` records the verbatim envelope.
- **A recorded argument that capture redacted cannot match** a replayed call
  carrying the real value; it fails closed as unmatched.
- **Replays do not write lineage edges.** Only `intervention` produces a capsule.
  Its `replay_of_run_id` becomes a `replayed_from` edge when that capsule is
  indexed.
- **No record-versus-replay output comparison runs automatically.** The signals
  are the subprocess exit code and status, environment warnings, schema drift,
  and, for `intervention`, its own checks. To compare the outputs of two runs,
  use `nova diff`.

## Related commands

| Command | Purpose | Maturity |
|---|---|---|
| `nova replay --dry-run` | Shows what would run, with the policy decision for each recorded tool call | works today |
| `nova replay-equivalence` | Compares two tool-call trajectories (`set`, `ordered` or `edit` match) and labels each divergence `dropped`, `added` or `changed` (`replay/equivalence/`) | experimental |
| `nova session replay` | Replays the members of a session in sequence | experimental |
| `nova evidence attest-replay` | Replays the run and writes a signed re-performance attestation | experimental |

## Read next

- [The lineage graph](lineage-graph.md): how `replayed_from` edges link runs.
- [Concepts › Replay Modes](../concepts.md#replay-modes)
