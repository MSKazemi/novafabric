"""Tests for nova api-proxy (Path A — ADR-0026 / v0.6.4).

The proxy listens on a TCP port, forwards POST requests to an upstream
LLM API, and records each call into model-calls.jsonl. Non-streaming
responses are captured fully; streaming responses are forwarded through
unchanged with streaming=true flag (full delta merge queued for v0.6.5).

Tests use the public ``serve_one_request_for_test`` seam plus a mocked
``httpx.post`` so they pass without binding sockets or hitting a real
LLM provider.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from novafabric.capture.capsule import CapsuleWriter
from novafabric.proxy.api_proxy import ApiProxy

RUN_ID = "01TESTAPIPROXY00000000000000"


def _writer(tmp_path: Path) -> CapsuleWriter:
    w = CapsuleWriter(run_id=RUN_ID, base_dir=tmp_path)
    w.open()
    return w


def _model_calls(tmp_path: Path) -> list[dict[str, Any]]:
    text = (tmp_path / RUN_ID / "model-calls.jsonl").read_text().strip()
    return [json.loads(line) for line in text.splitlines() if line]


def _mock_response(
    *, status: int = 200, body: bytes = b"", content_type: str = "application/json"
) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.content = body
    resp.headers = {"content-type": content_type}
    return resp


# ── Construction ─────────────────────────────────────────────────────────────


class TestConstruction:
    def test_rejects_non_http_upstream(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="must start with http"):
            ApiProxy(
                listen_host="127.0.0.1", listen_port=0,
                upstream_url="ftp://example.com",
                writer=_writer(tmp_path), parent_span_id="0" * 16,
            )

    def test_strips_trailing_slash(self, tmp_path: Path) -> None:
        proxy = ApiProxy(
            listen_host="127.0.0.1", listen_port=0,
            upstream_url="https://api.openai.com/",
            writer=_writer(tmp_path), parent_span_id="0" * 16,
        )
        assert proxy._upstream_url == "https://api.openai.com"


# ── Non-streaming OpenAI capture ─────────────────────────────────────────────


class TestNonStreamingOpenAI:
    def test_chat_completion_recorded_with_full_semconv(self, tmp_path: Path) -> None:
        proxy = ApiProxy(
            listen_host="127.0.0.1", listen_port=0,
            upstream_url="https://api.openai.com",
            writer=_writer(tmp_path), parent_span_id="aabbccddeeff0011",
        )
        request_body = json.dumps({
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "hi"}],
            "temperature": 0.7,
            "max_tokens": 256,
            "top_p": 0.95,
            "seed": 42,
        }).encode()
        upstream_body = json.dumps({
            "id": "chatcmpl-abc",
            "model": "gpt-4o-2024-08-06",
            "choices": [{"index": 0, "message": {
                "role": "assistant", "content": "hello"
            }, "finish_reason": "stop"}],
        }).encode()

        with patch("httpx.post", return_value=_mock_response(body=upstream_body)):
            status, body, ctype = proxy.serve_one_request_for_test(
                method="POST",
                path="/v1/chat/completions",
                headers={"content-type": "application/json"},
                body=request_body,
            )

        assert status == 200
        assert body == upstream_body
        rec = _model_calls(tmp_path)[0]
        # Request-side semconv populated.
        assert rec["gen_ai.system"] == "openai"
        assert rec["gen_ai.request.model"] == "gpt-4o"
        assert rec["gen_ai.request.temperature"] == 0.7
        assert rec["gen_ai.request.max_tokens"] == 256
        assert rec["gen_ai.request.top_p"] == 0.95
        assert rec["gen_ai.request.seed"] == 42
        assert rec["status"] == "success"
        assert rec["endpoint"] == "https://api.openai.com/v1/chat/completions"
        assert rec["parent_span_id"] == "aabbccddeeff0011"
        # api-proxy-specific extension.
        assert rec["extensions"]["io.novafabric.capture_method"] == "api-proxy"
        assert rec["extensions"]["io.novafabric.transport_kind"] == "http"

    def test_non_registered_url_passthrough_records_unknown_system(
        self, tmp_path: Path
    ) -> None:
        """If the upstream URL isn't in the URL registry, the proxy still
        forwards but records gen_ai.system='unknown'. Capture is best-
        effort; it never silently drops calls."""
        proxy = ApiProxy(
            listen_host="127.0.0.1", listen_port=0,
            upstream_url="https://internal.example.com",
            writer=_writer(tmp_path), parent_span_id="0" * 16,
        )
        request_body = json.dumps({
            "model": "internal-model",
            "messages": [],
        }).encode()
        with patch("httpx.post", return_value=_mock_response(
            body=b'{"choices": []}'
        )):
            proxy.serve_one_request_for_test(
                method="POST", path="/v1/chat",
                headers={}, body=request_body,
            )
        rec = _model_calls(tmp_path)[0]
        assert rec["gen_ai.system"] == "unknown"
        assert rec["gen_ai.request.model"] == "internal-model"

    def test_error_status_recorded_as_error(self, tmp_path: Path) -> None:
        proxy = ApiProxy(
            listen_host="127.0.0.1", listen_port=0,
            upstream_url="https://api.openai.com",
            writer=_writer(tmp_path), parent_span_id="0" * 16,
        )
        request_body = b'{"model":"gpt-4o","messages":[]}'
        with patch("httpx.post", return_value=_mock_response(
            status=429, body=b'{"error":{"message":"rate limit"}}'
        )):
            status, _, _ = proxy.serve_one_request_for_test(
                method="POST", path="/v1/chat/completions",
                headers={}, body=request_body,
            )
        assert status == 429
        assert _model_calls(tmp_path)[0]["status"] == "error"


# ── Bedrock body adapter is invoked end-to-end ───────────────────────────────


class TestBedrockBodyAdapter:
    def test_bedrock_anthropic_body_yields_correct_model(self, tmp_path: Path) -> None:
        """Path A's hot value: pointing api-proxy at Bedrock and getting a
        capture record with gen_ai.request.model populated correctly via
        the C-3.3b adapter, even though the body has no top-level model."""
        proxy = ApiProxy(
            listen_host="127.0.0.1", listen_port=0,
            upstream_url="https://bedrock-runtime.us-east-1.amazonaws.com",
            writer=_writer(tmp_path), parent_span_id="0" * 16,
        )
        bedrock_body = json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": "hi"}],
            "temperature": 0.5,
        }).encode()
        with patch("httpx.post", return_value=_mock_response(
            body=b'{"id":"msg-1"}'
        )):
            proxy.serve_one_request_for_test(
                method="POST",
                path="/model/anthropic.claude-haiku-4-5-v1:0/invoke",
                headers={}, body=bedrock_body,
            )
        rec = _model_calls(tmp_path)[0]
        # Adapter normalized the body before extraction:
        assert rec["gen_ai.system"] == "aws.bedrock"
        assert rec["gen_ai.request.model"] == "anthropic.claude-haiku-4-5-v1:0"
        assert rec["gen_ai.request.max_tokens"] == 1024
        assert rec["gen_ai.request.temperature"] == 0.5


# ── Streaming responses (forwarded through, recorded with flag) ──────────────


class TestStreamingDeltaMerge:
    """v0.6.5: walk an OpenAI SSE delta stream, synthesize a merged
    non-streaming response envelope (concatenate content, accumulate
    tool_calls, pull usage), record THAT as the response."""

    def _build_openai_stream(self, *events: dict[str, Any]) -> bytes:
        """Build an OpenAI-style SSE body terminated with [DONE]."""
        out: list[bytes] = []
        for e in events:
            out.append(b"data: " + json.dumps(e).encode() + b"\n\n")
        out.append(b"data: [DONE]\n\n")
        return b"".join(out)

    def test_content_deltas_concatenate(self, tmp_path: Path) -> None:
        proxy = ApiProxy(
            listen_host="127.0.0.1", listen_port=0,
            upstream_url="https://api.openai.com",
            writer=_writer(tmp_path), parent_span_id="0" * 16,
        )
        request_body = json.dumps({
            "model": "gpt-4o", "messages": [], "stream": True,
        }).encode()
        sse_body = self._build_openai_stream(
            {"id": "chatcmpl-x", "model": "gpt-4o-2024-08-06",
             "choices": [{"index": 0, "delta": {"role": "assistant", "content": "Hello"}}]},
            {"id": "chatcmpl-x", "model": "gpt-4o-2024-08-06",
             "choices": [{"index": 0, "delta": {"content": " world"}}]},
            {"id": "chatcmpl-x", "model": "gpt-4o-2024-08-06",
             "choices": [{"index": 0, "delta": {"content": "!"}, "finish_reason": "stop"}]},
        )
        with patch("httpx.post", return_value=_mock_response(
            body=sse_body, content_type="text/event-stream",
        )):
            proxy.serve_one_request_for_test(
                method="POST", path="/v1/chat/completions",
                headers={}, body=request_body,
            )
        rec = _model_calls(tmp_path)[0]
        choices = rec["gen_ai.response.choices"]
        assert len(choices) == 1
        assert choices[0]["message"]["content"] == "Hello world!"
        assert choices[0]["message"]["role"] == "assistant"
        assert choices[0]["finish_reason"] == "stop"
        assert rec["gen_ai.response.id"] == "chatcmpl-x"
        assert rec["gen_ai.response.model"] == "gpt-4o-2024-08-06"
        assert rec["gen_ai.response.finish_reasons"] == ["stop"]

    def test_usage_extracted_from_final_event(self, tmp_path: Path) -> None:
        proxy = ApiProxy(
            listen_host="127.0.0.1", listen_port=0,
            upstream_url="https://api.openai.com",
            writer=_writer(tmp_path), parent_span_id="0" * 16,
        )
        request_body = b'{"model":"gpt-4o","messages":[],"stream":true}'
        sse_body = self._build_openai_stream(
            {"id": "x", "model": "m",
             "choices": [{"index": 0, "delta": {"content": "hi"}}]},
            # Final event includes usage when stream_options.include_usage
            {"id": "x", "model": "m",
             "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
             "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}},
        )
        with patch("httpx.post", return_value=_mock_response(
            body=sse_body, content_type="text/event-stream",
        )):
            proxy.serve_one_request_for_test(
                method="POST", path="/v1/chat/completions",
                headers={}, body=request_body,
            )
        rec = _model_calls(tmp_path)[0]
        # Translated from OpenAI's prompt/completion to OTel input/output.
        assert rec["gen_ai.usage.input_tokens"] == 5
        assert rec["gen_ai.usage.output_tokens"] == 3

    def test_tool_call_deltas_accumulate(self, tmp_path: Path) -> None:
        """OpenAI streams tool_calls piecewise: index points to the slot,
        function.arguments concatenates as a JSON string."""
        proxy = ApiProxy(
            listen_host="127.0.0.1", listen_port=0,
            upstream_url="https://api.openai.com",
            writer=_writer(tmp_path), parent_span_id="0" * 16,
        )
        request_body = b'{"model":"gpt-4o","messages":[],"stream":true}'
        ev1 = {
            "id": "x", "model": "m",
            "choices": [{
                "index": 0,
                "delta": {
                    "role": "assistant",
                    "tool_calls": [{
                        "index": 0, "id": "call_abc", "type": "function",
                        "function": {"name": "get_weather", "arguments": ""},
                    }],
                },
            }],
        }
        ev2 = {
            "id": "x", "model": "m",
            "choices": [{
                "index": 0,
                "delta": {"tool_calls": [
                    {"index": 0, "function": {"arguments": '{"city":'}}
                ]},
            }],
        }
        ev3 = {
            "id": "x", "model": "m",
            "choices": [{
                "index": 0,
                "delta": {"tool_calls": [
                    {"index": 0, "function": {"arguments": '"Paris"}'}}
                ]},
                "finish_reason": "tool_calls",
            }],
        }
        sse_body = self._build_openai_stream(ev1, ev2, ev3)
        with patch("httpx.post", return_value=_mock_response(
            body=sse_body, content_type="text/event-stream",
        )):
            proxy.serve_one_request_for_test(
                method="POST", path="/v1/chat/completions",
                headers={}, body=request_body,
            )
        rec = _model_calls(tmp_path)[0]
        msg = rec["gen_ai.response.choices"][0]["message"]
        assert msg["tool_calls"][0]["id"] == "call_abc"
        assert msg["tool_calls"][0]["function"]["name"] == "get_weather"
        assert msg["tool_calls"][0]["function"]["arguments"] == '{"city":"Paris"}'
        assert rec["gen_ai.response.finish_reasons"] == ["tool_calls"]

    def test_done_sentinel_does_not_corrupt_record(self, tmp_path: Path) -> None:
        """The literal `data: [DONE]` event must not be parsed as JSON."""
        proxy = ApiProxy(
            listen_host="127.0.0.1", listen_port=0,
            upstream_url="https://api.openai.com",
            writer=_writer(tmp_path), parent_span_id="0" * 16,
        )
        ev = {"id": "x", "choices": [{
            "index": 0, "delta": {"content": "ok"}, "finish_reason": "stop",
        }]}
        sse_body = (
            b'data: ' + json.dumps(ev).encode() + b'\n\n'
            + b'data: [DONE]\n\n'
        )
        with patch("httpx.post", return_value=_mock_response(
            body=sse_body, content_type="text/event-stream",
        )):
            proxy.serve_one_request_for_test(
                method="POST", path="/v1/chat/completions",
                headers={}, body=b'{"model":"gpt-4o","messages":[],"stream":true}',
            )
        rec = _model_calls(tmp_path)[0]
        assert rec["gen_ai.response.choices"][0]["message"]["content"] == "ok"

    def test_malformed_event_skipped(self, tmp_path: Path) -> None:
        """A malformed event in the stream must not break the merge."""
        proxy = ApiProxy(
            listen_host="127.0.0.1", listen_port=0,
            upstream_url="https://api.openai.com",
            writer=_writer(tmp_path), parent_span_id="0" * 16,
        )
        ev_start = {"id": "x", "choices": [
            {"index": 0, "delta": {"content": "start"}}
        ]}
        ev_end = {"id": "x", "choices": [
            {"index": 0, "delta": {"content": " end"}, "finish_reason": "stop"}
        ]}
        sse_body = (
            b'data: ' + json.dumps(ev_start).encode() + b'\n\n'
            + b'data: not-json-at-all\n\n'
            + b'data: ' + json.dumps(ev_end).encode() + b'\n\n'
            + b'data: [DONE]\n\n'
        )
        with patch("httpx.post", return_value=_mock_response(
            body=sse_body, content_type="text/event-stream",
        )):
            proxy.serve_one_request_for_test(
                method="POST", path="/v1/chat/completions",
                headers={}, body=b'{"model":"gpt-4o","messages":[],"stream":true}',
            )
        rec = _model_calls(tmp_path)[0]
        # The merge survived the malformed event in the middle.
        assert rec["gen_ai.response.choices"][0]["message"]["content"] == "start end"

    def test_streaming_extension_still_set(self, tmp_path: Path) -> None:
        """The streaming extension (streamed=True, chunk_count) is set
        IN ADDITION to the merged response — consumers can tell whether
        the response was synthesized vs delivered as a single JSON."""
        proxy = ApiProxy(
            listen_host="127.0.0.1", listen_port=0,
            upstream_url="https://api.openai.com",
            writer=_writer(tmp_path), parent_span_id="0" * 16,
        )
        sse_body = self._build_openai_stream({
            "id": "x",
            "choices": [{
                "index": 0, "delta": {"content": "a"},
                "finish_reason": "stop",
            }],
        })
        with patch("httpx.post", return_value=_mock_response(
            body=sse_body, content_type="text/event-stream",
        )):
            proxy.serve_one_request_for_test(
                method="POST", path="/v1/chat/completions",
                headers={}, body=b'{"model":"gpt-4o","messages":[],"stream":true}',
            )
        rec = _model_calls(tmp_path)[0]
        ext = rec["extensions"]["io.novafabric.streaming"]
        assert ext["streamed"] is True
        assert ext["chunk_count"] >= 1


class TestAnthropicStreamingDeltaMerge:
    """v0.6.6: Anthropic uses a different SSE event-type model than
    OpenAI (message_start, content_block_delta, message_delta,
    message_stop). The merger walks these and synthesizes an OpenAI-
    shaped response choice for consumers."""

    def _build_anthropic_stream(self, *events: dict[str, Any]) -> bytes:
        out: list[bytes] = []
        for e in events:
            ev_type = e.get("type", "message")
            out.append(b"event: " + str(ev_type).encode() + b"\n")
            out.append(b"data: " + json.dumps(e).encode() + b"\n\n")
        return b"".join(out)

    def test_text_blocks_concatenate_with_usage_and_stop_reason(
        self, tmp_path: Path
    ) -> None:
        proxy = ApiProxy(
            listen_host="127.0.0.1", listen_port=0,
            upstream_url="https://api.anthropic.com",
            writer=_writer(tmp_path), parent_span_id="0" * 16,
        )
        request_body = json.dumps({
            "model": "claude-sonnet-4-7", "max_tokens": 100,
            "messages": [{"role": "user", "content": "hi"}], "stream": True,
        }).encode()
        sse_body = self._build_anthropic_stream(
            {"type": "message_start", "message": {
                "id": "msg_x", "model": "claude-sonnet-4-7-20260420",
                "content": [], "usage": {"input_tokens": 10},
            }},
            {"type": "content_block_start", "index": 0,
             "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "text_delta", "text": "Hello"}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "text_delta", "text": " world"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta",
             "delta": {"stop_reason": "end_turn"},
             "usage": {"output_tokens": 5}},
            {"type": "message_stop"},
        )
        with patch("httpx.post", return_value=_mock_response(
            body=sse_body, content_type="text/event-stream",
        )):
            proxy.serve_one_request_for_test(
                method="POST", path="/v1/messages",
                headers={}, body=request_body,
            )
        rec = _model_calls(tmp_path)[0]
        # Synthesized OpenAI-shaped choice.
        choices = rec["gen_ai.response.choices"]
        assert len(choices) == 1
        msg = choices[0]["message"]
        assert msg["role"] == "assistant"
        assert msg["content"] == "Hello world"
        assert choices[0]["finish_reason"] == "end_turn"
        assert rec["gen_ai.response.id"] == "msg_x"
        assert rec["gen_ai.response.model"] == "claude-sonnet-4-7-20260420"
        assert rec["gen_ai.response.finish_reasons"] == ["end_turn"]
        # Anthropic usage fields → OTel input/output.
        assert rec["gen_ai.usage.input_tokens"] == 10
        assert rec["gen_ai.usage.output_tokens"] == 5

    def test_tool_use_block_synthesized_as_tool_call(self, tmp_path: Path) -> None:
        """Anthropic tool_use blocks accumulate input_json_delta events
        and become OpenAI-style tool_calls in the synthesized choice."""
        proxy = ApiProxy(
            listen_host="127.0.0.1", listen_port=0,
            upstream_url="https://api.anthropic.com",
            writer=_writer(tmp_path), parent_span_id="0" * 16,
        )
        sse_body = self._build_anthropic_stream(
            {"type": "message_start", "message": {
                "id": "msg_y", "model": "claude-sonnet-4-7",
                "usage": {"input_tokens": 8},
            }},
            {"type": "content_block_start", "index": 0, "content_block": {
                "type": "tool_use", "id": "toolu_abc",
                "name": "get_weather", "input": {},
            }},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "input_json_delta", "partial_json": '{"city":'}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "input_json_delta", "partial_json": '"Paris"}'}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta",
             "delta": {"stop_reason": "tool_use"},
             "usage": {"output_tokens": 12}},
            {"type": "message_stop"},
        )
        with patch("httpx.post", return_value=_mock_response(
            body=sse_body, content_type="text/event-stream",
        )):
            proxy.serve_one_request_for_test(
                method="POST", path="/v1/messages",
                headers={}, body=b'{"model":"claude-sonnet-4-7","messages":[],"stream":true}',
            )
        rec = _model_calls(tmp_path)[0]
        msg = rec["gen_ai.response.choices"][0]["message"]
        assert msg["tool_calls"][0]["id"] == "toolu_abc"
        assert msg["tool_calls"][0]["function"]["name"] == "get_weather"
        # Arguments JSON re-assembled from partial_json deltas.
        args = json.loads(msg["tool_calls"][0]["function"]["arguments"])
        assert args == {"city": "Paris"}
        assert rec["gen_ai.response.finish_reasons"] == ["tool_use"]

    def test_multiple_text_blocks_in_order(self, tmp_path: Path) -> None:
        """If Anthropic emits multiple text blocks (e.g. interleaved with
        tool_use), text blocks are joined in index order."""
        proxy = ApiProxy(
            listen_host="127.0.0.1", listen_port=0,
            upstream_url="https://api.anthropic.com",
            writer=_writer(tmp_path), parent_span_id="0" * 16,
        )
        sse_body = self._build_anthropic_stream(
            {"type": "message_start", "message": {
                "id": "x", "model": "m", "usage": {"input_tokens": 1},
            }},
            {"type": "content_block_start", "index": 0,
             "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "text_delta", "text": "First. "}},
            {"type": "content_block_stop", "index": 0},
            {"type": "content_block_start", "index": 1,
             "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 1,
             "delta": {"type": "text_delta", "text": "Second."}},
            {"type": "content_block_stop", "index": 1},
            {"type": "message_delta",
             "delta": {"stop_reason": "end_turn"},
             "usage": {"output_tokens": 2}},
            {"type": "message_stop"},
        )
        with patch("httpx.post", return_value=_mock_response(
            body=sse_body, content_type="text/event-stream",
        )):
            proxy.serve_one_request_for_test(
                method="POST", path="/v1/messages",
                headers={}, body=b'{"model":"m","messages":[],"stream":true}',
            )
        rec = _model_calls(tmp_path)[0]
        assert rec["gen_ai.response.choices"][0]["message"]["content"] == (
            "First. Second."
        )

    def test_anthropic_merger_handles_malformed_event(self, tmp_path: Path) -> None:
        """A malformed event in the middle of the stream must not break
        the merge — the rest of the events still produce a record."""
        proxy = ApiProxy(
            listen_host="127.0.0.1", listen_port=0,
            upstream_url="https://api.anthropic.com",
            writer=_writer(tmp_path), parent_span_id="0" * 16,
        )
        sse_body = (
            b'event: message_start\n'
            b'data: ' + json.dumps({
                "type": "message_start",
                "message": {
                    "id": "x", "model": "m",
                    "usage": {"input_tokens": 1},
                },
            }).encode() + b'\n\n'
            b'event: garbage\n'
            b'data: not-json-at-all\n\n'
            b'event: content_block_start\n'
            b'data: ' + json.dumps({
                "type": "content_block_start", "index": 0,
                "content_block": {"type": "text", "text": ""},
            }).encode() + b'\n\n'
            b'event: content_block_delta\n'
            b'data: ' + json.dumps({
                "type": "content_block_delta", "index": 0,
                "delta": {"type": "text_delta", "text": "ok"},
            }).encode() + b'\n\n'
            b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
        )
        with patch("httpx.post", return_value=_mock_response(
            body=sse_body, content_type="text/event-stream",
        )):
            proxy.serve_one_request_for_test(
                method="POST", path="/v1/messages",
                headers={}, body=b'{"model":"m","messages":[],"stream":true}',
            )
        rec = _model_calls(tmp_path)[0]
        assert rec["gen_ai.response.choices"][0]["message"]["content"] == "ok"

    def test_anthropic_tool_use_with_malformed_input_json(self, tmp_path: Path) -> None:
        """If input_json_delta accumulates to invalid JSON (e.g. truncated
        stream), the tool_call's arguments must still parse to a JSON
        string of an empty dict — never crash."""
        proxy = ApiProxy(
            listen_host="127.0.0.1", listen_port=0,
            upstream_url="https://api.anthropic.com",
            writer=_writer(tmp_path), parent_span_id="0" * 16,
        )
        sse_body = self._build_anthropic_stream(
            {"type": "message_start", "message": {
                "id": "x", "model": "m", "usage": {"input_tokens": 1},
            }},
            {"type": "content_block_start", "index": 0, "content_block": {
                "type": "tool_use", "id": "t1", "name": "fn", "input": {},
            }},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "input_json_delta", "partial_json": '{not'}},
            {"type": "message_stop"},
        )
        with patch("httpx.post", return_value=_mock_response(
            body=sse_body, content_type="text/event-stream",
        )):
            proxy.serve_one_request_for_test(
                method="POST", path="/v1/messages",
                headers={}, body=b'{"model":"m","messages":[],"stream":true}',
            )
        rec = _model_calls(tmp_path)[0]
        # tool_calls present, arguments parsed to {} on JSON-decode failure.
        msg = rec["gen_ai.response.choices"][0]["message"]
        assert msg["tool_calls"][0]["function"]["arguments"] == "{}"

    def test_dispatcher_picks_anthropic_for_anthropic_url(
        self, tmp_path: Path
    ) -> None:
        """The shared _merge_streaming_response dispatcher routes by
        gen_ai_system. URL → registry → 'anthropic' → anthropic merger."""
        from novafabric.proxy.api_proxy import _merge_streaming_response

        # OpenAI-formatted body sent to gen_ai_system='anthropic' would
        # NOT be merged correctly — that's the point of dispatch.
        # Use an actual Anthropic stream to confirm the dispatch chose
        # the anthropic merger (which knows about 'content' not 'choices').
        sse_body = self._build_anthropic_stream(
            {"type": "message_start", "message": {
                "id": "x", "model": "m", "usage": {"input_tokens": 1},
            }},
            {"type": "content_block_start", "index": 0,
             "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "text_delta", "text": "hi"}},
            {"type": "message_stop"},
        )
        merged = _merge_streaming_response(sse_body, "anthropic")
        # Anthropic shape: content + (no choices).
        assert "content" in merged
        assert merged["content"][0]["text"] == "hi"
        assert "choices" not in merged


class TestStreamingResponses:
    def test_sse_response_forwarded_with_streaming_flag(self, tmp_path: Path) -> None:
        """Streaming OpenAI responses are SSE deltas. v0.6.4 forwards the
        bytes through unchanged (preserves client UX) and records the
        request with streaming=true. Full delta merge is queued for v0.6.5."""
        proxy = ApiProxy(
            listen_host="127.0.0.1", listen_port=0,
            upstream_url="https://api.openai.com",
            writer=_writer(tmp_path), parent_span_id="0" * 16,
        )
        request_body = json.dumps({
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        }).encode()
        sse_body = (
            b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n'
            b'data: {"choices":[{"delta":{"content":" world"}}]}\n\n'
            b'data: [DONE]\n\n'
        )
        with patch("httpx.post", return_value=_mock_response(
            body=sse_body, content_type="text/event-stream",
        )):
            status, body, ctype = proxy.serve_one_request_for_test(
                method="POST", path="/v1/chat/completions",
                headers={"accept": "text/event-stream"}, body=request_body,
            )
        # Bytes pass through unchanged.
        assert body == sse_body
        assert ctype == "text/event-stream"
        rec = _model_calls(tmp_path)[0]
        # Request-side semconv still recorded.
        assert rec["gen_ai.request.model"] == "gpt-4o"
        # Streaming extension set.
        streaming = rec["extensions"]["io.novafabric.streaming"]
        assert streaming["streamed"] is True
        assert streaming["chunk_count"] >= 2  # at least 2 data events


# ── Upstream connectivity errors ─────────────────────────────────────────────


class TestUpstreamErrors:
    def test_unreachable_returns_502_and_records_error(self, tmp_path: Path) -> None:
        import httpx

        proxy = ApiProxy(
            listen_host="127.0.0.1", listen_port=0,
            upstream_url="https://api.openai.com",
            writer=_writer(tmp_path), parent_span_id="0" * 16,
        )
        with patch("httpx.post", side_effect=httpx.ConnectError("nope")):
            status, body, ctype = proxy.serve_one_request_for_test(
                method="POST", path="/v1/chat/completions",
                headers={}, body=b'{"model":"gpt-4o","messages":[]}',
            )
        assert status == 502
        envelope = json.loads(body)
        assert "error" in envelope
        rec = _model_calls(tmp_path)[0]
        assert rec["status"] == "error"


# ── GET requests (e.g. /v1/models discovery) — forward but don't record ──────


class TestNonPostRequests:
    def test_get_request_is_forwarded_not_recorded(self, tmp_path: Path) -> None:
        """OpenAI clients sometimes call GET /v1/models for discovery.
        Forward without recording — these aren't model calls."""
        proxy = ApiProxy(
            listen_host="127.0.0.1", listen_port=0,
            upstream_url="https://api.openai.com",
            writer=_writer(tmp_path), parent_span_id="0" * 16,
        )
        with patch("httpx.request", return_value=_mock_response(
            body=b'{"data":[]}'
        )) as mock_req:
            status, body, _ = proxy.serve_one_request_for_test(
                method="GET", path="/v1/models",
                headers={}, body=b"",
            )
        assert status == 200
        assert mock_req.called
        # No record for GET.
        try:
            assert _model_calls(tmp_path) == []
        except FileNotFoundError:
            # model-calls.jsonl never written = also fine
            pass


# ── Auth header forwarding (transparent proxy must pass auth through) ────────


class TestAuthHeaderForwarding:
    def test_authorization_header_forwarded_to_upstream(
        self, tmp_path: Path
    ) -> None:
        """The proxy is transparent — clients send Authorization (e.g. a
        bearer token) and the proxy must forward it. Otherwise upstream
        returns 401 and the proxy is useless."""
        proxy = ApiProxy(
            listen_host="127.0.0.1", listen_port=0,
            upstream_url="https://api.openai.com",
            writer=_writer(tmp_path), parent_span_id="0" * 16,
        )
        captured_kwargs: dict[str, Any] = {}

        def capture(*args: Any, **kwargs: Any) -> MagicMock:
            captured_kwargs.update(kwargs)
            return _mock_response(body=b'{"choices":[]}')

        with patch("httpx.post", side_effect=capture):
            proxy.serve_one_request_for_test(
                method="POST", path="/v1/chat/completions",
                headers={
                    "authorization": "Bearer sk-test-key",
                    "content-type": "application/json",
                },
                body=b'{"model":"gpt-4o","messages":[]}',
            )
        # The authorization header MUST be in the forwarded headers.
        headers = captured_kwargs.get("headers") or {}
        # Headers can be either case; check both.
        keys_lower = {k.lower(): v for k, v in headers.items()}
        assert "authorization" in keys_lower
        assert keys_lower["authorization"] == "Bearer sk-test-key"


# ── Server-loop bind/shutdown smoke ──────────────────────────────────────────


class TestServerLoop:
    def test_run_binds_and_shuts_down_cleanly(self, tmp_path: Path) -> None:
        import socket
        import threading
        import time as _time

        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()

        proxy = ApiProxy(
            listen_host="127.0.0.1", listen_port=port,
            upstream_url="https://api.openai.com",
            writer=_writer(tmp_path), parent_span_id="0" * 16,
        )

        result_holder: dict[str, int] = {}

        def runner_thread() -> None:
            result_holder["exit"] = proxy.run()

        t = threading.Thread(target=runner_thread, daemon=True)
        t.start()
        deadline = _time.monotonic() + 2.0
        while proxy._server is None and _time.monotonic() < deadline:
            _time.sleep(0.01)
        assert proxy._server is not None
        threading.Thread(target=proxy._server.shutdown, daemon=True).start()
        t.join(timeout=2.0)
        assert not t.is_alive()
        assert result_holder["exit"] == 0


# ── No prompt rewriting (proxy is record-only per ADR-0026) ──────────────────


class TestRequestBodyParsing:
    def test_body_that_parses_to_non_dict_recorded_safely(
        self, tmp_path: Path
    ) -> None:
        """A request body that parses to JSON but isn't a dict (e.g. an
        array, a string, a number) must still produce a record — capture
        is a recorder, not a validator."""
        proxy = ApiProxy(
            listen_host="127.0.0.1", listen_port=0,
            upstream_url="https://api.openai.com",
            writer=_writer(tmp_path), parent_span_id="0" * 16,
        )
        # Top-level array — valid JSON, not a dict.
        request_body = b'["not","a","dict"]'
        with patch("httpx.post", return_value=_mock_response(
            body=b'{"choices":[]}',
        )):
            proxy.serve_one_request_for_test(
                method="POST", path="/v1/chat/completions",
                headers={}, body=request_body,
            )
        rec = _model_calls(tmp_path)[0]
        # Empty body_dict → defaults; semconv extractor returns
        # 'unknown' for missing model.
        assert rec["gen_ai.request.model"] == "unknown"
        assert rec["gen_ai.request.messages"] == []

    def test_completely_unparseable_body_recorded_safely(
        self, tmp_path: Path
    ) -> None:
        """A request body that isn't even valid JSON (e.g. raw form data)
        must still produce a record."""
        proxy = ApiProxy(
            listen_host="127.0.0.1", listen_port=0,
            upstream_url="https://api.openai.com",
            writer=_writer(tmp_path), parent_span_id="0" * 16,
        )
        with patch("httpx.post", return_value=_mock_response(
            body=b'{"choices":[]}',
        )):
            proxy.serve_one_request_for_test(
                method="POST", path="/v1/chat/completions",
                headers={"content-type": "application/x-www-form-urlencoded"},
                body=b'key1=value1&key2=value2',
            )
        rec = _model_calls(tmp_path)[0]
        assert rec["gen_ai.request.model"] == "unknown"


class TestNoRewriting:
    def test_request_body_byte_identical_to_upstream(self, tmp_path: Path) -> None:
        """ADR-0026 anti-pattern: proxy must never rewrite prompts. Verify
        the bytes that go upstream are bit-identical to what the client sent."""
        proxy = ApiProxy(
            listen_host="127.0.0.1", listen_port=0,
            upstream_url="https://api.openai.com",
            writer=_writer(tmp_path), parent_span_id="0" * 16,
        )
        original_bytes = json.dumps({
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "do not modify this"}],
            "temperature": 0.42,
        }).encode()
        forwarded: dict[str, bytes] = {}

        def capture(url: str, **kwargs: Any) -> MagicMock:
            forwarded["content"] = kwargs.get("content", b"")
            return _mock_response(body=b'{"choices":[]}')

        with patch("httpx.post", side_effect=capture):
            proxy.serve_one_request_for_test(
                method="POST", path="/v1/chat/completions",
                headers={}, body=original_bytes,
            )
        assert forwarded["content"] == original_bytes
