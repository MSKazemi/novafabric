"""The mocked-replay support matrix, as data (ADR-0300, ADR-0304, issue #16).

``docs/architecture/replay-modes.md`` carries this table between generated-block
markers; ``scripts/gen_replay_support_matrix.py`` renders it and
``tests/replay/test_support_matrix_is_generated.py`` fails when the page drifts
from this module **or** when a row's claim drifts from the dispatcher's real
patch tables (``SERVED_MODEL_SURFACES`` / ``UNSUPPORTED_MODEL_SURFACES``) or
names an evidence test that does not exist.

Edit the rows here, never the page.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

#: (module, class, attribute) of an SDK method the dispatcher patches.
PatchTarget = tuple[str, str, str]

Replay = Literal["served", "refused", "not intercepted", "capture only"]


@dataclass(frozen=True)
class SurfaceRow:
    """One provider/API surface: what capture records and what replay does."""

    surface: str
    capture: str
    replay: Replay
    replay_note: str
    streaming: str
    asynchronous: str
    status: str
    #: ``tests/<path>.py::<test name>`` entries (validated), or plain prose.
    evidence: tuple[str, ...]
    #: The SDK methods this row speaks for; must match the dispatcher tables.
    patches: tuple[PatchTarget, ...] = field(default_factory=tuple)
    #: Tool surfaces this row speaks for that are served by a *registered
    #: server* rather than a patch (ADR-0306); must match the surfaces
    #: ``MockToolDispatcher.install`` registers.
    servers: tuple[str, ...] = field(default_factory=tuple)


_E2E = "tests/replay/test_model_surface_coverage_e2e.py"
_CONTRACT = "tests/replay/test_mocked_replay_contract.py"
_ROUND_TRIP = "tests/replay/test_tool_choice_round_trip_e2e.py"
_CAPTURE = "tests/capture/test_sdk_stream_capture.py"
_ERRORS = "tests/replay/test_recorded_model_errors_replay.py"
_ENDINGS = "tests/replay/test_undelivered_stream_endings.py"
_PY_TOOL = "tests/replay/test_python_tool_replay_e2e.py"
_OVERRIDES = "tests/replay/test_tool_overrides_enforced_e2e.py"
_SLICE4 = "tests/replay/test_slice4_matching_and_echo_contract.py"
_ECHO = "tests/replay/test_echo_and_nested_coverage_e2e.py"
_ADK = "tests/replay/test_adk_tool_replay_e2e.py"
_OAI = "tests/replay/test_openai_agents_tool_replay_e2e.py"
_HF = "tests/capture/test_hf_inference_capture.py"
_SMOL = "tests/adapters/test_smolagents.py"
_UNSERVED = "tests/replay/test_unservable_model_surfaces_e2e.py"
_OLLAMA = "tests/replay/test_ollama_replay_e2e.py"
_BEDROCK = "tests/replay/test_bedrock_replay_e2e.py"
_BEDROCK_WIRE = "tests/capture/test_bedrock_wire_capture.py"
_BEDROCK_SERVE = "tests/replay/test_bedrock_serving.py"
_LC = "tests/replay/test_langchain_tool_replay_e2e.py"
_LC_OBSERVED = "tests/replay/test_langchain_unwrapped_tool_accounting_e2e.py"
_OAI_OBSERVED = "tests/replay/test_openai_agents_unwrapped_tool_e2e.py"
_CHILDNET = "tests/replay/test_child_process_network.py"
_HERMETIC = "tests/replay/test_hermetic_replay.py"
_LG_ADAPTER = "tests/replay/test_langgraph_adapter_replay_e2e.py"

ROWS: tuple[SurfaceRow, ...] = (
    SurfaceRow(
        surface="OpenAI Chat Completions `create`",
        capture=(
            "SDK hook: full response incl. `tool_calls`; a streamed response is "
            "folded into one record (`nova.streaming`)"
        ),
        replay="served",
        replay_note="",
        streaming=(
            "served: the record is replayed as chunks (usage chunk when requested); "
            "a stream that never delivered a finish reason is recorded with "
            "`finish_reason: null` and served without a finish-reason or usage chunk"
        ),
        asynchronous="served",
        status="works today",
        evidence=(
            f"{_ROUND_TRIP}::test_openai_tool_calls_round_trip_through_capture_and_mocked_replay",
            f"{_E2E}::test_s14_async_chat_completions_round_trip",
            f"{_E2E}::test_s15_streamed_chat_completions_round_trip",
            f"{_E2E}::test_s15_async_streamed_chat_round_trip",
            f"{_ENDINGS}::test_a_chat_stream_that_never_finished_is_recorded_and_replayed_without_one",
        ),
        patches=(
            ("openai.resources.chat.completions", "Completions", "create"),
            ("openai.resources.chat.completions", "AsyncCompletions", "create"),
        ),
    ),
    SurfaceRow(
        surface="OpenAI `chat.completions.stream()` helper",
        capture="through `create(stream=True)`",
        replay="served",
        replay_note="the SDK's own stream accumulator runs on the replayed chunks",
        streaming="served",
        asynchronous="same path, not tested",
        status="works today",
        evidence=(f"{_E2E}::test_s15_chat_stream_helper_round_trip",),
    ),
    SurfaceRow(
        surface="OpenAI Responses API `create`",
        capture=(
            "SDK hook: output text and `function_call` items (as `tool_calls`), "
            "marked `io.novafabric.api_surface: openai.responses`; streamed calls "
            "recorded from the terminal event"
        ),
        replay="served",
        replay_note=(
            "rebuilt `Response`; reasoning and hosted-tool output items are not "
            "recorded, so not served"
        ),
        streaming=(
            "served: the record is replayed as the event sequence, incl. "
            "`responses.stream()`"
        ),
        asynchronous="served",
        status="works today",
        evidence=(
            f"{_E2E}::test_s16_responses_api_round_trip_with_function_call",
            f"{_E2E}::test_s16_streamed_responses_api_round_trip",
            f"{_CONTRACT}::test_s11_a_chat_recording_is_not_served_to_the_responses_api",
        ),
        patches=(
            ("openai.resources.responses", "Responses", "create"),
            ("openai.resources.responses", "AsyncResponses", "create"),
        ),
    ),
    SurfaceRow(
        surface="OpenAI `chat.completions.parse`, `responses.parse`",
        capture="wire hook: request only",
        replay="refused",
        replay_note="",
        streaming="—",
        asynchronous="refused",
        status="unsupported",
        evidence=(f"{_CONTRACT}::test_s17_unsupported_model_surfaces_are_refused",),
        patches=(
            ("openai.resources.chat.completions", "Completions", "parse"),
            ("openai.resources.chat.completions", "AsyncCompletions", "parse"),
            ("openai.resources.responses", "Responses", "parse"),
            ("openai.resources.responses", "AsyncResponses", "parse"),
        ),
    ),
    SurfaceRow(
        surface="OpenAI legacy `completions.create`",
        capture="wire hook: request only",
        replay="refused",
        replay_note="",
        streaming="refused",
        asynchronous="refused",
        status="unsupported",
        evidence=(f"{_CONTRACT}::test_s17_unsupported_model_surfaces_are_refused",),
        patches=(
            ("openai.resources.completions", "Completions", "create"),
            ("openai.resources.completions", "AsyncCompletions", "create"),
        ),
    ),
    SurfaceRow(
        surface="`with_raw_response` / `with_streaming_response` (any served surface)",
        capture="recorded without a response",
        replay="refused",
        replay_note="the caller expects an HTTP response wrapper, which replay cannot build",
        streaming="refused",
        asynchronous="refused",
        status="unsupported",
        evidence=(
            f"{_CONTRACT}::test_s17_unsupported_model_surfaces_are_refused",
            f"{_CAPTURE}::test_raw_response_calls_are_recorded_without_choices",
        ),
    ),
    SurfaceRow(
        surface="Anthropic Messages `create`",
        capture=(
            "SDK hook: full response incl. `tool_use`; finish reason mapped to the "
            "schema enum, raw value kept; a streamed response is folded into one record"
        ),
        replay="served",
        replay_note="raw `stop_reason` served back",
        streaming=(
            "served: the record is replayed as raw stream events; no `message_delta` "
            "/ `message_stop` when the stream delivered no `stop_reason` "
            "(`finish_reason: null`)"
        ),
        asynchronous="served",
        status=(
            "works today (tested against a stand-in `anthropic` package; the real "
            "SDK is not a dependency)"
        ),
        evidence=(
            f"{_ROUND_TRIP}::test_anthropic_tool_use_round_trips_through_capture_and_mocked_replay",
            f"{_E2E}::test_s14_async_anthropic_messages_round_trip",
            f"{_E2E}::test_s15_streamed_anthropic_messages_round_trip",
            f"{_ENDINGS}::test_an_anthropic_stream_without_a_stop_reason_is_recorded_and_replayed_without_one",
        ),
        patches=(
            ("anthropic.resources.messages", "Messages", "create"),
            ("anthropic.resources.messages", "AsyncMessages", "create"),
        ),
    ),
    SurfaceRow(
        surface="Anthropic `messages.stream()` helper",
        capture="not recorded with a response (it bypasses `create`)",
        replay="refused",
        replay_note="",
        streaming="refused",
        asynchronous="refused",
        status="unsupported",
        evidence=(f"{_CONTRACT}::test_s17_unsupported_model_surfaces_are_refused",),
        patches=(
            ("anthropic.resources.messages", "Messages", "stream"),
            ("anthropic.resources.messages", "AsyncMessages", "stream"),
        ),
    ),
    SurfaceRow(
        surface="Anthropic `beta.messages`",
        capture="wire hook: request only",
        replay="refused",
        replay_note="",
        streaming="refused",
        asynchronous="refused",
        status="unsupported",
        evidence=("code: `UNSUPPORTED_MODEL_SURFACES` (real SDK not installed in CI)",),
        patches=(
            ("anthropic.resources.beta.messages", "Messages", "create"),
            ("anthropic.resources.beta.messages", "AsyncMessages", "create"),
            ("anthropic.resources.beta.messages", "Messages", "stream"),
            ("anthropic.resources.beta.messages", "AsyncMessages", "stream"),
        ),
    ),
    SurfaceRow(
        surface=(
            "Recorded model errors on any served surface (rate limit, 4xx, 5xx "
            "after the SDK's retries, timeout, connection error)"
        ),
        capture=(
            "SDK hook: one logical error record with the exception class, status, "
            "parsed body, request id and retry/rate-limit headers "
            "(`io.novafabric.sdk_error`); each HTTP attempt is a transport record"
        ),
        replay="served",
        replay_note=(
            "the same SDK exception class is raised at the recorded position "
            "(allow-listed classes only); transport attempts are never served; an "
            "error that cannot be rebuilt faithfully is refused"
        ),
        streaming=(
            "served: raised at `create` when the SDK raised there; when the stream "
            "failed part-way (an in-stream error event, a dropped connection), the "
            "delivered content is served without closing or terminal events, then "
            "the exception is raised"
        ),
        asynchronous="served",
        status=(
            "works today (OpenAI: real SDK; Anthropic: stand-in package); capsules "
            "captured before the error detail was recorded are refused"
        ),
        evidence=(
            f"{_ERRORS}::test_rate_limit_is_replayed_as_rate_limit_error_with_status_429",
            f"{_ERRORS}::test_a_call_retried_twice_then_successful_is_served_as_the_success",
            f"{_ERRORS}::test_recorded_errors_replay_on_every_openai_surface",
            f"{_ERRORS}::test_recorded_anthropic_errors_are_replayed",
            f"{_ERRORS}::test_an_unknown_error_class_fails_closed",
            f"{_ERRORS}::test_legacy_capsule_replays_until_its_unrebuildable_error_then_fails_closed",
            f"{_ERRORS}::test_an_error_event_mid_stream_is_replayed_after_the_delivered_chunks",
            f"{_ERRORS}::test_a_connection_dropped_mid_stream_is_replayed_after_the_delivered_chunks",
            f"{_ERRORS}::test_a_responses_stream_dropped_mid_way_is_replayed",
            f"{_ERRORS}::test_an_anthropic_error_mid_stream_is_replayed",
            f"{_ERRORS}::test_a_mid_stream_error_that_cannot_be_rebuilt_is_refused_before_any_chunk",
        ),
    ),
    SurfaceRow(
        surface=(
            "OpenAI Responses API response with `status: failed` or `incomplete`, "
            "or a stream that delivered an `error` event"
        ),
        capture=(
            "SDK hook: the Response's own `status`, `incomplete_details` and `error`, "
            "verbatim (`io.novafabric.response_status`); `failed` is also "
            "`status: error` on the record; a yielded `error` event is kept verbatim "
            "(`io.novafabric.stream_error_event`, `status: error`)"
        ),
        replay="served",
        replay_note=(
            "returned, never raised -- the SDK returns these responses and yields "
            "the `error` event; the recorded status, reason and error are served as "
            "recorded"
        ),
        streaming="served: ends with the recorded terminal event (`response.failed`, "
                  "`response.incomplete`); after an `error` event, the delivered "
                  "events, then that event, and no terminal event",
        asynchronous="served",
        status=(
            "works today; a failed response captured before its status was recorded "
            "is refused (re-capture)"
        ),
        evidence=(
            f"{_ERRORS}::test_a_failed_responses_response_is_returned_not_raised",
            f"{_ERRORS}::test_an_incomplete_responses_response_keeps_its_recorded_reason",
            f"{_ERRORS}::test_a_failed_response_captured_before_its_status_was_recorded_is_refused",
            f"{_ENDINGS}::test_a_responses_error_event_is_recorded_and_replayed_as_delivered",
        ),
    ),
    SurfaceRow(
        surface="Non-Python clients via `nova api-proxy`",
        capture=(
            "streaming: merged response, canonical `tool_calls`; non-streaming: "
            "request + id/model only"
        ),
        replay="capture only",
        replay_note="replay patches Python SDKs",
        streaming="—",
        asynchronous="—",
        status="capture only",
        evidence=("`tests/test_api_proxy.py`",),
    ),
    SurfaceRow(
        surface="Hugging Face `InferenceClient` / `AsyncInferenceClient` (router, Endpoints)",
        capture=(
            "wire hook, the router and Endpoints hosts (any provider route): request, requested "
            "model and selector, status, timing; the response is never read, so reported model, "
            "served provider and token counts are not recorded"
        ),
        replay="refused",
        replay_note=(
            "not intercepted and recorded without a response, so a strict replay **refuses "
            "to start** (`ModelSurfaceNotServable`, ADR-0319); `--permissive` runs it **live** "
            "and reports it in `model_calls_not_servable` and `network_connections_live`"
        ),
        streaming="request recorded; stream not read",
        asynchronous="request recorded",
        status="capture only",
        evidence=(
            f"{_HF}::test_a_hugging_face_record_is_never_a_replayable_model_call",
            f"{_HF}::test_only_the_two_hugging_face_inference_hosts_are_classified",
            f"{_HF}::test_a_streamed_response_is_not_read_by_the_hook",
            f"{_HF}::test_real_inference_client_sync_chat_is_recorded_once_with_honest_identity",
        ),
    ),
    SurfaceRow(
        surface="Hugging Face `smolagents` tool calls (`novafabric.adapters.smolagents`)",
        capture=(
            "observed per agent step: tool name, argument digest, step outcome; the framework "
            "reports no per-call result, and a `CodeAgent` step is one `python_interpreter` call"
        ),
        replay="not intercepted",
        replay_note="runs **live**; the record carries a surface marker replay does not intercept",
        streaming="run stream preserved; capsule closes when it ends or is abandoned",
        asynchronous="—",
        status="capture only",
        evidence=(
            f"{_SMOL}::test_tool_records_are_observe_only_and_never_a_replay_surface",
            f"{_SMOL}::test_a_real_code_agent_records_one_python_interpreter_call_and_never_the_code",
        ),
    ),
    SurfaceRow(
        surface=(
            "Ollama `/api/chat`, `/api/generate` over `httpx`/`httpx2` (raw HTTP, the "
            "`ollama` SDK, `langchain_ollama`; ADR-0320)"
        ),
        capture=(
            "wire hook: the response too -- text, `tool_calls`, `done_reason`, "
            "`prompt_eval_count`/`eval_count`, marked `io.novafabric.api_surface: "
            "ollama.chat` / `ollama.generate`; a streamed (NDJSON) body is copied as "
            "the workload reads it and recorded when the read ends"
        ),
        replay="served",
        replay_note=(
            "at the httpx transport, for registry-matched Ollama hosts (incl. "
            "`OLLAMA_BASE_URL`/`OLLAMA_HOST` at call time) and hosts the capsule "
            "recorded; one queue per endpoint, in order; exhaustion and endpoint "
            "mismatch fail closed; records captured before ADR-0320 (no response) are "
            "never served; `/api/embed` is refused like any unserved model call; "
            "`/api/tags`, `/api/show`, `/api/version` and `/api/ps` run no model, so they "
            "are not refused: they run live and their connections are reported (ADR-0319)"
        ),
        streaming=(
            "served as NDJSON in the recorded chunks, ending with the `done` object; "
            "without it when the capture never received one"
        ),
        asynchronous="served",
        status="experimental",
        evidence=(
            f"{_OLLAMA}::test_ollama_chat_and_generate_round_trip_streamed_and_not",
            f"{_OLLAMA}::test_a_tool_calling_ollama_response_round_trips",
            f"{_OLLAMA}::test_an_abandoned_stream_is_recorded_as_such_and_replayed_as_delivered",
            f"{_OLLAMA}::test_ollama_queue_exhaustion_fails_closed",
            f"{_OLLAMA}::test_a_surface_mismatch_fails_closed",
            f"{_OLLAMA}::test_a_record_without_a_response_is_never_served_empty",
            f"{_OLLAMA}::test_the_ollama_sdk_round_trips",
            f"{_OLLAMA}::test_langchain_ollama_round_trips",
        ),
        patches=(
            ("httpx", "HTTPTransport", "handle_request"),
            ("httpx", "AsyncHTTPTransport", "handle_async_request"),
            ("httpx2", "HTTPTransport", "handle_request"),
            ("httpx2", "AsyncHTTPTransport", "handle_async_request"),
        ),
    ),
    SurfaceRow(
        surface=(
            "AWS Bedrock `Converse` / `ConverseStream` through `boto3`/botocore "
            "(`bedrock-runtime` client, `langchain_aws.ChatBedrockConverse`)"
        ),
        capture=(
            "urllib3 wire hook: the response too -- text and `toolUse` blocks (canonical "
            "`tool_calls`), `stopReason`, `usage`, the block layout and the rest of the "
            "body; `modelId` from the URL, `inferenceConfig`, `system`/`toolConfig`; marked "
            "`io.novafabric.api_surface: aws.bedrock.converse` / `.converse_stream`; the body "
            "is copied as botocore reads it and recorded when the read ends; SigV4 headers "
            "are never recorded"
        ),
        replay="served",
        replay_note=(
            "at urllib3 `HTTPConnectionPool.urlopen`, for `bedrock-runtime.*` hosts, "
            "`AWS_ENDPOINT_URL_BEDROCK_RUNTIME`/`AWS_ENDPOINT_URL_BEDROCK` at call time and "
            "hosts the capsule recorded; botocore parses the served bytes; one queue per "
            "API, in order, signatures ignored; a recorded HTTP error (e.g. a throttle "
            "botocore retried) is served back as recorded; exhaustion and Converse/"
            "ConverseStream mismatch fail closed; `InvokeModel` is recorded without a "
            "response and refused; control-plane (`bedrock`) calls run no model: not "
            "refused, they run live"
        ),
        streaming=(
            "`ConverseStream`: the recorded events re-encoded as an AWS event stream; an "
            "abandoned stream is flagged `stream_complete: false` and served as delivered"
        ),
        asynchronous="— (boto3 is sync; aiobotocore over aiohttp is not served)",
        status="experimental",
        evidence=(
            f"{_BEDROCK}::test_converse_and_converse_stream_round_trip",
            f"{_BEDROCK}::test_a_tool_use_turn_and_its_tool_result_round_trip",
            f"{_BEDROCK}::test_a_different_tool_result_is_reported_by_the_echo_check",
            f"{_BEDROCK}::test_an_abandoned_stream_is_recorded_as_such_and_replayed_as_delivered",
            f"{_BEDROCK}::test_a_recorded_http_error_and_a_throttled_retry_are_served_as_recorded",
            f"{_BEDROCK}::test_bedrock_queue_exhaustion_fails_closed",
            f"{_BEDROCK}::test_converse_stream_where_converse_was_recorded_fails_closed",
            f"{_BEDROCK}::test_invoke_model_is_recorded_without_a_response_and_refused",
            f"{_BEDROCK}::test_a_control_plane_call_runs_no_model_and_is_not_refused",
            f"{_BEDROCK}::test_langchain_chat_bedrock_converse_with_tools_round_trips",
            f"{_BEDROCK_WIRE}::test_an_unread_body_is_not_read_ahead_and_is_recorded_when_read",
            f"{_BEDROCK_SERVE}::test_a_converse_stream_record_is_served_back_event_for_event",
        ),
        patches=(("urllib3.connectionpool", "HTTPConnectionPool", "urlopen"),),
    ),
    SurfaceRow(
        surface=(
            "Other providers' SDKs, raw HTTP to a model API (any registry host), "
            "or a served surface whose response was not recorded"
        ),
        capture="wire hook: request only",
        replay="refused",
        replay_note=(
            "not intercepted, so a strict replay **refuses to start** "
            "(`ModelSurfaceNotServable`, exit 1, also under `--dry-run`, which lists "
            "each as `[LIVE] model call (<system>, <host>)`); `--permissive` runs it "
            "**live** and reports `model_calls_not_servable` per `gen_ai.system` "
            "(ADR-0319)"
        ),
        streaming="—",
        asynchronous="—",
        status="not controlled; refused by default",
        evidence=(
            f"{_UNSERVED}::test_p7_strict_replay_refuses_before_spawning",
            f"{_UNSERVED}::test_p7_permissive_runs_live_and_reports_it",
            f"{_UNSERVED}::test_p7_dry_run_lists_the_live_model_call",
            f"{_UNSERVED}::test_mixed_capsule_names_only_the_unserved_system",
        ),
    ),
    SurfaceRow(
        surface="MCP `ClientSession.call_tool` (in-process hook or `nova mcp-proxy`)",
        capture="full result (proxy: verbatim JSON-RPC envelope)",
        replay="served",
        replay_note=(
            "one-to-one; an unmatched call is refused (`--permissive` runs it live only "
            "with `--allow-unknown-mutation`: MCP calls count as `unknown`); "
            "`replay.yaml` `tool_overrides` enforced in-process (experimental, ADR-0306); "
            "arguments matched after secret redaction, raw-equal records first, so an "
            "argument the capsule scanner redacted still matches (experimental, ADR-0306 "
            "slice 4)"
        ),
        streaming="—",
        asynchronous="(async by nature)",
        status="works today",
        evidence=(
            f"{_CONTRACT}::test_s2_model_tool_model",
            f"{_CONTRACT}::test_s4_repeated_identical_calls_consume_distinct_records",
            f"{_CONTRACT}::test_s8_missing_tool_record_fails_closed_even_if_the_workload_swallows_it",
            f"{_CONTRACT}::test_s13_permissive_refuses_an_unmatched_mcp_call_without_the_ladder_flag",
            f"{_OVERRIDES}::test_dry_run_report_equals_replayed_behaviour",
            f"{_SLICE4}::test_an_mcp_call_whose_argument_the_scanner_redacted_is_served",
            f"{_SLICE4}::test_an_unsealed_capsule_matches_exactly_as_before_redact_then_hash",
        ),
        patches=(("mcp.client.session", "ClientSession", "call_tool"),),
    ),
    SurfaceRow(
        surface="Python function declared with `novafabric.capture.record.tool` (ADR-0306)",
        capture=(
            "one `transport: python` record per call; arguments and result kept only at "
            "the `forensic`/`air_gapped` capture level, digests otherwise"
        ),
        replay="served",
        replay_note=(
            "before the function body runs; one-to-one by name and canonical arguments; "
            "JSON-native results up to 1 MiB; an unmatched or unservable call is refused "
            "(`--permissive` runs it live only if a ladder flag permits its declared "
            "mutation class); `replay.yaml` `tool_overrides` enforced in-process; a "
            "boundary that called a model or tool is served too, and the records it "
            "wrote are consumed as covered (`*_calls_covered`, slice 4)"
        ),
        streaming="refused at decoration (generator functions)",
        asynchronous="served (`async def`)",
        status="experimental",
        evidence=(
            f"{_PY_TOOL}::test_decorated_calls_are_served_and_their_bodies_never_run",
            f"{_PY_TOOL}::test_positional_keyword_and_default_calls_match_and_consume_in_order",
            f"{_PY_TOOL}::test_an_unmatched_call_fails_closed_before_the_body_runs",
            f"{_PY_TOOL}::test_an_unservable_record_fails_closed_naming_the_cause",
            f"{_PY_TOOL}::test_permissive_refuses_an_unmatched_unknown_call_without_the_ladder_flag",
            f"{_OVERRIDES}::test_an_unmatched_intercepted_call_follows_the_owner_rules",
            f"{_PY_TOOL}::test_a_nested_boundary_is_served_and_its_inner_records_are_covered",
            f"{_ECHO}::test_a_nested_boundary_is_served_and_its_nested_model_call_is_covered",
            f"{_ECHO}::test_an_unmarked_nested_call_fails_closed",
        ),
        servers=("novafabric.capture.record.tool",),
    ),
    SurfaceRow(
        surface=(
            "Google ADK tool, through the NovaFabric tool plugin "
            "(`novafabric.adapters.google_adk.make_tool_plugin`, ADR-0306 slice 3)"
        ),
        capture=(
            "one `transport: python` record per ADK tool call (`google.adk.tool` "
            "marker, `function_call_id`); arguments and result kept only at the "
            "`forensic`/`air_gapped` capture level, digests otherwise"
        ),
        replay="served",
        replay_note=(
            "through ADK's documented `before_tool_callback` short-circuit, before the "
            "tool body runs; one-to-one by the model's `function_call_id` (same name "
            "and arguments) or by name and canonical arguments; JSON-native results up "
            "to 1 MiB; a call that failed, changed `tool_context.actions`, is "
            "long-running or made nested model/tool calls is not served; an unmatched "
            "or unservable call is refused (`--permissive` runs it live only if a "
            "ladder flag permits the class the plugin declares); `replay.yaml` "
            "`tool_overrides` enforced; `nova capture python …` capsules (a "
            "`make_google_adk_plugin()` capsule is re-run only from a recorded process "
            "invocation that passes every guard, ADR-0306 Q7, experimental -- not tested "
            "end to end for ADK)"
        ),
        streaming="not served (long-running tools)",
        asynchronous="served (ADK runs every tool on its event loop)",
        status="experimental",
        evidence=(
            f"{_ADK}::test_adk_tool_calls_are_served_and_their_bodies_never_run",
            f"{_ADK}::test_an_unmatched_adk_call_fails_closed_before_the_tool_runs",
            f"{_ADK}::test_the_function_call_id_pairs_identical_calls_by_id",
            f"{_ADK}::test_permissive_runs_an_unmatched_tool_live_only_with_the_ladder",
            f"{_ADK}::test_a_replay_yaml_allow_false_override_holds_under_permissive",
            f"{_ADK}::test_a_nested_record_makes_the_adk_call_unservable",
        ),
        servers=("google.adk.tools.BaseTool",),
    ),
    SurfaceRow(
        surface=(
            "OpenAI Agents SDK function tool, through the NovaFabric tool seam "
            "(`novafabric.adapters.openai_agents.wrap_tools`, ADR-0306 slice 3)"
        ),
        capture=(
            "one `transport: python` record per function-tool call (`openai.agents.tool` "
            "marker, the model's `call_id`); arguments and result kept only at the "
            "`forensic`/`air_gapped` capture level, digests otherwise"
        ),
        replay="served",
        replay_note=(
            "by replacing `FunctionTool.on_invoke_tool` on a copy of each tool -- an "
            "**undocumented SDK seam**, checked against `openai-agents` 0.17.x only -- so "
            "the recorded result is returned before the tool body runs; one-to-one by the "
            "model's `call_id` (same name and arguments) or by name and canonical "
            "arguments; JSON-native results up to 1 MiB; a call that raised, or made "
            "nested model/tool calls (including `Agent.as_tool`), is not served; an "
            "unmatched or unservable call is refused (`--permissive` runs it live only if "
            "a ladder flag permits the class declared in `wrap_tools`); `replay.yaml` "
            "`tool_overrides` enforced; tools not passed through `wrap_tools`: next "
            "row; `nova capture python …` capsules (a `register()` capsule is re-run "
            "only from a recorded process invocation that passes every guard, ADR-0306 Q7, "
            "experimental -- not tested end to end for the Agents SDK)"
        ),
        streaming="served (`Runner.run_streamed` reaches the same seam)",
        asynchronous="served (the SDK awaits every tool on its event loop)",
        status="experimental",
        evidence=(
            f"{_OAI}::test_agents_tool_calls_are_served_and_their_bodies_never_run",
            f"{_OAI}::test_an_unmatched_agents_call_fails_closed_before_the_tool_runs",
            f"{_OAI}::test_the_call_id_pairs_identical_calls_by_id",
            f"{_OAI}::test_permissive_runs_an_unmatched_tool_live_only_with_the_ladder",
            f"{_OAI}::test_a_replay_yaml_allow_false_override_holds_under_permissive",
            f"{_OAI}::test_a_nested_record_makes_the_agents_call_unservable",
        ),
        servers=("agents.FunctionTool",),
    ),
    SurfaceRow(
        surface=(
            "OpenAI Agents SDK function tool **not** passed through `wrap_tools` "
            "(`@function_tool`, `FunctionTool`)"
        ),
        capture=(
            "recorded automatically when `agents` is imported (no code change): one "
            "`transport: python` record per call (`openai.agents.tool` marker, "
            "`io.novafabric.openai_agents_tool_seam: observed`, `mutation_class: "
            "unknown`), same payload and codec rules as `wrap_tools`; agents used as "
            "tools (`Agent.as_tool`) are not recorded -- their own model calls and tools "
            "are"
        ),
        replay="served",
        replay_note=(
            "**served by default** when the record holds a servable result (ADR-0306 r3 "
            "amendment, owner decision 2026-11-21; before, these tools were neither "
            "recorded nor counted and ran live): the same matcher, codec and refusals "
            "as a `wrap_tools` tool declared `unknown`; a capsule whose observed records "
            "cannot be served makes a strict replay **refuse to start** "
            "(`ToolSurfaceNotServed`, also under `--dry-run`); an unmatched call (e.g. a "
            "capsule captured before this, with no tool records) is refused before its "
            "body runs; under `--permissive` it runs **live** only with "
            "`--allow-unknown-mutation`, counted in `tool_calls_live`; the seam patches "
            "the SDK's private `agents.tool._invoke_function_tool_with_metadata` -- "
            "**not an SDK extension point**, checked against `openai-agents` 0.24.0 only"
        ),
        streaming="— (not exercised by the e2e: `Runner.run` only)",
        asynchronous="served (the SDK awaits every tool on its event loop)",
        status="experimental",
        evidence=(
            f"{_OAI_OBSERVED}::test_capture_records_unwrapped_function_tools_as_observed",
            f"{_OAI_OBSERVED}::test_an_agent_used_as_a_tool_is_not_a_boundary_its_inner_tool_is",
            f"{_OAI_OBSERVED}::test_strict_replay_serves_unwrapped_function_tools_and_never_runs_their_bodies",
            f"{_OAI_OBSERVED}::test_strict_replay_refuses_to_start_when_unwrapped_results_were_not_recorded",
            f"{_OAI_OBSERVED}::test_a_capsule_without_tool_records_refuses_strict_and_counts_permissive",
            f"{_OAI_OBSERVED}::test_wrapped_and_unwrapped_function_tools_one_record_per_call",
        ),
        servers=("agents.FunctionTool",),
    ),
    SurfaceRow(
        surface=(
            "LangChain / LangGraph tool (`BaseTool`: `@tool`, `StructuredTool`, "
            "subclasses), through the NovaFabric tool seam "
            "(`novafabric.adapters.langchain_tools.wrap_tools`, ADR-0306 slice 3)"
        ),
        capture=(
            "one `transport: python` record per tool call (`langchain.tool` marker, the "
            "model's `tool_call_id`); arguments the model sent (injected state, store, "
            "runtime and call id left out) and the tool body's result kept only at the "
            "`forensic`/`air_gapped` capture level, digests otherwise"
        ),
        replay="served",
        replay_note=(
            "by a generated subclass of each tool's class, on a copy, that overrides "
            "`run`/`arun` and the body hooks `_run`/`_arun` -- **not a LangChain "
            "extension point**, checked against `langchain-core` 1.8.3 and `langgraph` "
            "1.2.23 only -- so the recorded body result is returned before the body runs "
            "and LangChain still builds the `ToolMessage`; direct `invoke`/`ainvoke` and "
            "LangGraph `ToolNode`; one-to-one by the model's `tool_call_id` (same name "
            "and arguments) or by name and canonical arguments; JSON-native results up "
            "to 1 MiB (a `Command`, a `ToolMessage` or a `content_and_artifact` tuple is "
            "not); a call that raised or made nested model/tool calls is not served; an "
            "unmatched or unservable call is refused (`--permissive` runs it live only "
            "if a ladder flag permits the class declared in `wrap_tools`); `replay.yaml` "
            "`tool_overrides` enforced; tools not passed through `wrap_tools`: next "
            "row (`langchain_mcp_adapters` tools are left to the MCP surface); "
            "`nova capture python …` capsules, and LangGraph "
            "`wrap()` capsules re-run from their recorded process invocation when every "
            "guard passes (ADR-0306 Q7, experimental)"
        ),
        streaming="— (tools are not streamed)",
        asynchronous="served (`ainvoke`/`arun`, async tools, `ToolNode` in an async graph)",
        status="experimental",
        evidence=(
            f"{_LC}::test_langchain_tool_calls_are_served_and_their_bodies_never_run",
            f"{_LC}::test_a_langgraph_agent_with_a_served_openai_model_replays_faithfully",
            f"{_LC}::test_an_unmatched_langchain_call_fails_closed_before_the_tool_runs",
            f"{_LC}::test_the_tool_call_id_pairs_identical_calls_by_id",
            f"{_LC}::test_permissive_runs_an_unmatched_tool_live_only_with_the_ladder",
            f"{_LC}::test_a_replay_yaml_allow_false_override_holds_under_permissive",
            f"{_LC}::test_a_nested_record_makes_the_langchain_call_unservable",
            f"{_LG_ADAPTER}::test_wrapped_langchain_tools_are_recorded_and_served_in_an_adapter_capsule",
        ),
        servers=("langchain_core.tools.BaseTool",),
    ),
    SurfaceRow(
        surface=(
            "LangChain / LangGraph tool **not** passed through `wrap_tools` (`@tool`, "
            "`StructuredTool`, `BaseTool` subclasses run by `invoke`/`ainvoke`/`run`/"
            "`arun` or LangGraph `ToolNode`)"
        ),
        capture=(
            "recorded automatically when `langchain_core` is imported (no code "
            "change): one `transport: python` record per tool body execution "
            "(`langchain.tool` marker, `io.novafabric.langchain_tool_seam: observed`, "
            "`mutation_class: unknown`), same payload and codec rules as `wrap_tools` "
            "(values at `forensic`/`air_gapped` only, digests otherwise)"
        ),
        replay="served",
        replay_note=(
            "**served by default** when the record holds a servable result (ADR-0306 "
            "r3 amendment, owner decision 2026-11-21; round 2 never served these): the "
            "same one-to-one matcher, codec and refusals as a `wrap_tools` tool "
            "declared `unknown` -- the body never runs; a capsule whose observed "
            "records cannot be served (captured below `forensic`, so no result was "
            "kept; a failure; a non-JSON result) makes a strict replay **refuse to "
            "start** (`ToolSurfaceNotServed`, exit 1, also under `--dry-run`, which "
            "lists each as `[NOT SERVED] LangChain tool call` with its reason, and "
            "says to capture again); an unmatched call (e.g. a capsule that recorded "
            "no tool calls) is refused before its body runs; under `--permissive` an "
            "unservable or unmatched call runs **live** only with "
            "`--allow-unknown-mutation`, counted in `tool_calls_live` and "
            "`replay_contract.tool_calls_live_by_tool`; a round-2 observed record "
            "that kept its value is served without a re-capture; `allow: false` "
            "refuses an unservable call before its body runs; the seam patches "
            "`BaseTool.run`/`arun` and the class defining `_run`/`_arun` -- **not a "
            "LangChain extension point**, checked against `langchain-core` 1.8.3 and "
            "`langgraph` 1.2.23 only"
        ),
        streaming="— (tools are not streamed)",
        asynchronous="served (`ainvoke`/`arun`, async tools, `ToolNode` in an async graph)",
        status="experimental",
        evidence=(
            f"{_LC_OBSERVED}::test_capture_records_langchain_tools_that_were_not_wrapped",
            f"{_LC_OBSERVED}::test_a_forensic_capture_makes_an_unwrapped_tool_record_servable",
            f"{_LC_OBSERVED}::test_strict_replay_serves_recorded_unwrapped_tool_calls_and_never_runs_their_bodies",
            f"{_LC_OBSERVED}::test_an_unmatched_unwrapped_call_is_refused_strict_before_its_body_runs",
            f"{_LC_OBSERVED}::test_strict_replay_refuses_to_start_when_unwrapped_tool_results_were_not_recorded",
            f"{_LC_OBSERVED}::test_permissive_runs_unservable_unwrapped_tools_live_only_with_allow_unknown_mutation",
            f"{_LC_OBSERVED}::test_an_r2_observed_record_with_its_payload_is_served",
            f"{_LC_OBSERVED}::test_a_capsule_without_tool_records_refuses_strict_and_counts_permissive",
            f"{_LC_OBSERVED}::test_wrapped_and_unwrapped_tools_are_both_served_one_record_per_call",
            f"{_LC_OBSERVED}::test_an_allow_false_override_stops_an_unwrapped_tool_before_its_body_runs",
            f"{_LG_ADAPTER}::test_an_unwrapped_tool_in_an_adapter_capsule_is_served_strict",
        ),
        servers=("langchain_core.tools.BaseTool",),
    ),
    SurfaceRow(
        surface="MCP session set-up (server start, `initialize`, `list_tools`)",
        capture="not recorded as tool calls",
        replay="not intercepted",
        replay_note="runs **live**",
        streaming="—",
        asynchronous="—",
        status="not controlled",
        evidence=("e2e tests start a real in-memory MCP server during replay",),
    ),
    SurfaceRow(
        surface=(
            "Undeclared functions the workload runs for a model (the result goes back "
            "as a tool message)"
        ),
        capture=(
            "the model's tool choice, and the result as the next request sent it back "
            "(`role: tool` message / Anthropic `tool_result` block)"
        ),
        replay="not intercepted",
        replay_note=(
            "runs **live**; the result it sends back is compared with the recorded one "
            "(redacted digests, by `tool_call_id`): a difference is reported in "
            "`replay_contract.tool_result_echo` and never fails the replay "
            "(report-only, experimental, ADR-0306 D10); not checked for the Responses "
            "API, whose request input is not recorded"
        ),
        streaming="—",
        asynchronous="—",
        status="not controlled",
        evidence=(
            f"{_ECHO}::test_a_changed_tool_result_is_reported_but_never_fails_the_replay",
            f"{_ECHO}::test_a_capsule_without_request_messages_reports_not_checked_never_matched",
            f"{_SLICE4}::test_the_echo_check_compares_anthropic_tool_results",
        ),
    ),
    SurfaceRow(
        surface=(
            "HTTP, shell, filesystem, framework-native tools "
            "(other than Google ADK tools through the tool plugin, OpenAI Agents "
            "function tools, and LangChain tools)"
        ),
        capture="network/file events, not tool records",
        replay="not intercepted",
        replay_note=(
            "runs **live**; outbound connections of the replayed Python process and "
            "of its Python children -- also those whose environment drops the replay's "
            "variables, such as MCP stdio servers -- are reported "
            "(`network_connections_live`, `network_observation_scope: python-process`), "
            "files are not; child processes are counted and named "
            "(`child_processes_spawned`, `child_processes_argv0`), and one that is not "
            "observed (a non-Python program) is counted in `child_processes_unobserved` "
            "with `network_observation_coverage: partial` (ADR-0319); `--hermetic` "
            "(experimental, Linux, ADR-0322) blocks the network of the whole process "
            "tree instead; a `replay.yaml` `allow: false` override on one makes a strict "
            "replay refuse to start (`ToolOverrideUnenforceable`, exit 3)"
        ),
        streaming="—",
        asynchronous="—",
        status="not controlled",
        evidence=(
            f"{_CONTRACT}::test_s13_network_tool_refused_and_uncontrolled_transports_reported",
            f"{_CONTRACT}::test_s13_live_network_from_an_uncontrolled_tool_is_reported_not_blocked",
            f"{_OVERRIDES}::test_strict_replay_refuses_to_start_on_an_unenforceable_override",
            f"{_UNSERVED}::test_a_child_process_is_counted_and_named_with_a_warning",
            f"{_CHILDNET}::test_an_mcp_stdio_server_child_is_observed",
            f"{_CHILDNET}::"
            "test_a_non_python_child_is_counted_unobserved_and_the_count_is_a_lower_bound",
            f"{_HERMETIC}::test_hermetic_replay_reaches_no_host_service_from_any_process",
        ),
    ),
)

_REPLAY_LABEL: dict[str, str] = {
    "served": "**Served**",
    "refused": "Refused",
    "not intercepted": "Not intercepted",
    "capture only": "Not intercepted",
}

BEGIN_MARKER = (
    "<!-- BEGIN GENERATED: replay-support-matrix "
    "(scripts/gen_replay_support_matrix.py) -->"
)
END_MARKER = "<!-- END GENERATED: replay-support-matrix -->"


def _evidence_cell(evidence: tuple[str, ...]) -> str:
    parts = []
    for item in evidence:
        if "::" in item:
            path, name = item.split("::", 1)
            parts.append(f"`{path.rsplit('/', 1)[-1]}::{name}`")
        else:
            parts.append(item)
    return "; ".join(parts)


def render_table() -> str:
    """The matrix as a Markdown table (deterministic)."""
    lines = [
        "| Provider / API surface | Capture | Mocked replay | Streaming | Async | Status "
        "| Evidence |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in ROWS:
        replay = _REPLAY_LABEL[row.replay]
        if row.replay_note:
            replay += f" — {row.replay_note}"
        lines.append(
            f"| {row.surface} | {row.capture} | {replay} | {row.streaming} | "
            f"{row.asynchronous} | {row.status} | {_evidence_cell(row.evidence)} |"
        )
    return "\n".join(lines)


def render_block() -> str:
    """The generated block, markers included, as it appears in the docs page."""
    return f"{BEGIN_MARKER}\n\n{render_table()}\n\n{END_MARKER}"
