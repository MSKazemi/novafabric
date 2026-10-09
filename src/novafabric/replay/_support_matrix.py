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
        surface="Other providers' SDKs, raw HTTP to a model API",
        capture="wire hook: request only",
        replay="not intercepted",
        replay_note="runs **live**; its connections are reported (`network_connections_live`)",
        streaming="—",
        asynchronous="—",
        status="not controlled",
        evidence=(
            f"{_CONTRACT}::test_s13_live_network_from_an_uncontrolled_tool_is_reported_not_blocked",
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
        surface="HTTP, shell, filesystem, framework-native tools",
        capture="network/file events, not tool records",
        replay="not intercepted",
        replay_note=(
            "runs **live**; outbound connections are reported "
            "(`network_connections_live`), files and processes are not; a "
            "`replay.yaml` `allow: false` override on one makes a strict replay refuse "
            "to start (`ToolOverrideUnenforceable`, exit 3)"
        ),
        streaming="—",
        asynchronous="—",
        status="not controlled",
        evidence=(
            f"{_CONTRACT}::test_s13_network_tool_refused_and_uncontrolled_transports_reported",
            f"{_CONTRACT}::test_s13_live_network_from_an_uncontrolled_tool_is_reported_not_blocked",
            f"{_OVERRIDES}::test_strict_replay_refuses_to_start_on_an_unenforceable_override",
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
