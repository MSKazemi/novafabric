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


_E2E = "tests/replay/test_model_surface_coverage_e2e.py"
_CONTRACT = "tests/replay/test_mocked_replay_contract.py"
_ROUND_TRIP = "tests/replay/test_tool_choice_round_trip_e2e.py"
_CAPTURE = "tests/capture/test_sdk_stream_capture.py"
_ERRORS = "tests/replay/test_recorded_model_errors_replay.py"

ROWS: tuple[SurfaceRow, ...] = (
    SurfaceRow(
        surface="OpenAI Chat Completions `create`",
        capture=(
            "SDK hook: full response incl. `tool_calls`; a streamed response is "
            "folded into one record (`nova.streaming`)"
        ),
        replay="served",
        replay_note="",
        streaming="served: the record is replayed as chunks (usage chunk when requested)",
        asynchronous="served",
        status="works today",
        evidence=(
            f"{_ROUND_TRIP}::test_openai_tool_calls_round_trip_through_capture_and_mocked_replay",
            f"{_E2E}::test_s14_async_chat_completions_round_trip",
            f"{_E2E}::test_s15_streamed_chat_completions_round_trip",
            f"{_E2E}::test_s15_async_streamed_chat_round_trip",
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
        streaming="served: the record is replayed as raw stream events",
        asynchronous="served",
        status=(
            "works today (tested against a stand-in `anthropic` package; the real "
            "SDK is not a dependency)"
        ),
        evidence=(
            f"{_ROUND_TRIP}::test_anthropic_tool_use_round_trips_through_capture_and_mocked_replay",
            f"{_E2E}::test_s14_async_anthropic_messages_round_trip",
            f"{_E2E}::test_s15_streamed_anthropic_messages_round_trip",
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
        surface="OpenAI Responses API response with `status: failed` or `incomplete`",
        capture=(
            "SDK hook: the Response's own `status`, `incomplete_details` and `error`, "
            "verbatim (`io.novafabric.response_status`); `failed` is also "
            "`status: error` on the record"
        ),
        replay="served",
        replay_note=(
            "returned, never raised -- the SDK returns these responses; the "
            "recorded status, reason and error are served as recorded"
        ),
        streaming="served: ends with the recorded terminal event (`response.failed`, "
                  "`response.incomplete`)",
        asynchronous="served",
        status=(
            "works today; a failed response captured before its status was recorded "
            "is refused (re-capture)"
        ),
        evidence=(
            f"{_ERRORS}::test_a_failed_responses_response_is_returned_not_raised",
            f"{_ERRORS}::test_an_incomplete_responses_response_keeps_its_recorded_reason",
            f"{_ERRORS}::test_a_failed_response_captured_before_its_status_was_recorded_is_refused",
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
        replay_note="one-to-one; an unmatched call is refused",
        streaming="—",
        asynchronous="(async by nature)",
        status="works today",
        evidence=(
            f"{_CONTRACT}::test_s2_model_tool_model",
            f"{_CONTRACT}::test_s4_repeated_identical_calls_consume_distinct_records",
            f"{_CONTRACT}::test_s8_missing_tool_record_fails_closed_even_if_the_workload_swallows_it",
        ),
        patches=(("mcp.client.session", "ClientSession", "call_tool"),),
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
        surface="HTTP, shell, filesystem, framework-native tools",
        capture="network/file events, not tool records",
        replay="not intercepted",
        replay_note=(
            "runs **live**; outbound connections are reported "
            "(`network_connections_live`), files and processes are not"
        ),
        streaming="—",
        asynchronous="—",
        status="not controlled",
        evidence=(
            f"{_CONTRACT}::test_s13_network_tool_refused_and_uncontrolled_transports_reported",
            f"{_CONTRACT}::test_s13_live_network_from_an_uncontrolled_tool_is_reported_not_blocked",
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
