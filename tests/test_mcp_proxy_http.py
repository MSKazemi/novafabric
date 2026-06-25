"""Tests for the MCP HTTP/SSE proxy (ADR-0015 §Secondary; C-3.4 / v0.6.3).

These tests use the public ``serve_one_request_for_test`` seam plus a
mocked ``httpx.post`` so they pass without binding sockets or having an
upstream MCP server. The end-to-end serving loop (BaseHTTPRequestHandler
+ ThreadingHTTPServer) is well-tested by stdlib; we test our own logic.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from novafabric.capture.capsule import CapsuleWriter
from novafabric.proxy.mcp_proxy import (
    MCPHttpProxy,
    _aggregate_sse_for_response,
    _count_sse_events,
)

RUN_ID = "01TESTHTTPPROXY00000000000000"


def _writer(tmp_path: Path) -> CapsuleWriter:
    w = CapsuleWriter(run_id=RUN_ID, base_dir=tmp_path)
    w.open()
    return w


def _tool_calls(tmp_path: Path) -> list[dict[str, Any]]:
    text = (tmp_path / RUN_ID / "tool-calls.jsonl").read_text().strip()
    return [json.loads(line) for line in text.splitlines() if line]


def _mock_response(
    *, status: int = 200, body: bytes = b"", content_type: str = "application/json"
) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.content = body
    resp.headers = {"content-type": content_type}
    return resp


# ── Construction guardrails ──────────────────────────────────────────────────


class TestConstruction:
    def test_rejects_non_http_upstream_url(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="must start with http"):
            MCPHttpProxy(
                listen_host="127.0.0.1", listen_port=0,
                upstream_url="mcp://localhost",
                writer=_writer(tmp_path), parent_span_id="0" * 16,
            )

    def test_strips_trailing_slash_from_upstream(self, tmp_path: Path) -> None:
        proxy = MCPHttpProxy(
            listen_host="127.0.0.1", listen_port=0,
            upstream_url="http://upstream.local/mcp/",
            writer=_writer(tmp_path), parent_span_id="0" * 16,
        )
        # Internal field; this is the only place we cross the implementation
        # boundary, to confirm the URL is normalized before forwarding.
        assert proxy._upstream_url == "http://upstream.local/mcp"


# ── tools/call over JSON-only response ───────────────────────────────────────


class TestJsonResponse:
    def test_tools_call_with_json_response_is_recorded(self, tmp_path: Path) -> None:
        proxy = MCPHttpProxy(
            listen_host="127.0.0.1", listen_port=0,
            upstream_url="http://upstream/mcp",
            writer=_writer(tmp_path), parent_span_id="aabbccddeeff0011",
        )
        request = json.dumps({
            "jsonrpc": "2.0", "id": "req-1",
            "method": "tools/call",
            "params": {"name": "echo", "arguments": {"text": "hi"}},
        }).encode()
        upstream_response_body = json.dumps({
            "jsonrpc": "2.0", "id": "req-1",
            "result": {"content": [{"type": "text", "text": "hi"}]},
        }).encode()

        with patch("httpx.post", return_value=_mock_response(
            body=upstream_response_body, content_type="application/json",
        )):
            status, body, ctype = proxy.serve_one_request_for_test(request)

        assert status == 200
        assert body == upstream_response_body
        assert ctype == "application/json"

        records = _tool_calls(tmp_path)
        assert len(records) == 1
        rec = records[0]
        assert rec["tool_name"] == "echo"
        assert rec["arguments"] == {"text": "hi"}
        assert rec["transport"] == "mcp"
        assert rec["status"] == "success"
        assert rec["extensions"]["io.novafabric.transport_kind"] == "http"
        assert rec["extensions"]["io.novafabric.capture_method"] == "proxy"
        # No streaming extension when the response wasn't an SSE stream.
        assert "io.novafabric.streaming" not in rec["extensions"]
        # parent_span_id propagated.
        assert rec["parent_span_id"] == "aabbccddeeff0011"

    def test_tools_call_error_response_records_status_error(
        self, tmp_path: Path
    ) -> None:
        proxy = MCPHttpProxy(
            listen_host="127.0.0.1", listen_port=0,
            upstream_url="http://upstream/mcp",
            writer=_writer(tmp_path), parent_span_id="0" * 16,
        )
        request = json.dumps({
            "jsonrpc": "2.0", "id": 42,
            "method": "tools/call",
            "params": {"name": "explode", "arguments": {}},
        }).encode()
        error_response = json.dumps({
            "jsonrpc": "2.0", "id": 42,
            "error": {"code": -32000, "message": "tool failed"},
        }).encode()

        with patch("httpx.post", return_value=_mock_response(body=error_response)):
            proxy.serve_one_request_for_test(request)

        rec = _tool_calls(tmp_path)[0]
        assert rec["status"] == "error"
        assert rec["error"]["message"] == "tool failed"

    def test_non_tools_call_request_is_forwarded_but_not_recorded(
        self, tmp_path: Path
    ) -> None:
        proxy = MCPHttpProxy(
            listen_host="127.0.0.1", listen_port=0,
            upstream_url="http://upstream/mcp",
            writer=_writer(tmp_path), parent_span_id="0" * 16,
        )
        # `ping` request — not tools/call. Should pass through; no record.
        request = json.dumps({
            "jsonrpc": "2.0", "id": "p1", "method": "ping",
        }).encode()
        upstream_pong = json.dumps({
            "jsonrpc": "2.0", "id": "p1", "result": {},
        }).encode()

        with patch("httpx.post", return_value=_mock_response(body=upstream_pong)):
            status, body, _ctype = proxy.serve_one_request_for_test(request)

        assert status == 200
        assert body == upstream_pong
        assert _tool_calls(tmp_path) == []


# ── tools/call over SSE response (the C-3.4 design point) ────────────────────


class TestSseResponse:
    def _make_sse_body(self, *messages: dict[str, Any]) -> bytes:
        """Build an SSE body from a sequence of JSON-RPC messages."""
        out: list[bytes] = []
        for m in messages:
            out.append(b"event: message\n")
            out.append(b"data: " + json.dumps(m).encode() + b"\n")
            out.append(b"\n")
        return b"".join(out)

    def test_sse_response_is_aggregated_into_one_record(
        self, tmp_path: Path
    ) -> None:
        """The defining test for C-3.4: an SSE response with multiple
        progress events plus a final response yields exactly one
        tool-call record."""
        proxy = MCPHttpProxy(
            listen_host="127.0.0.1", listen_port=0,
            upstream_url="http://upstream/mcp",
            writer=_writer(tmp_path), parent_span_id="0" * 16,
        )
        request = json.dumps({
            "jsonrpc": "2.0", "id": "stream-1",
            "method": "tools/call",
            "params": {"name": "long_running", "arguments": {}},
        }).encode()
        # 3 progress notifications (no id) + 1 final response (id matches).
        sse_body = self._make_sse_body(
            {"jsonrpc": "2.0", "method": "notifications/progress", "params": {"step": 1}},
            {"jsonrpc": "2.0", "method": "notifications/progress", "params": {"step": 2}},
            {"jsonrpc": "2.0", "method": "notifications/progress", "params": {"step": 3}},
            {"jsonrpc": "2.0", "id": "stream-1",
             "result": {"content": [{"type": "text", "text": "done"}]}},
        )

        with patch("httpx.post", return_value=_mock_response(
            body=sse_body, content_type="text/event-stream",
        )):
            status, body, ctype = proxy.serve_one_request_for_test(request)

        assert status == 200
        # The original SSE body is forwarded to the client unchanged.
        assert body == sse_body
        assert ctype == "text/event-stream"

        records = _tool_calls(tmp_path)
        assert len(records) == 1, (
            f"expected exactly 1 record (aggregated, not per-chunk), got {len(records)}"
        )
        rec = records[0]
        # The recorded response_envelope is the FINAL message (matched by id),
        # not the progress notifications.
        assert rec["mcp"]["response_envelope"]["id"] == "stream-1"
        assert rec["mcp"]["response_envelope"]["result"]["content"][0]["text"] == "done"
        # The streaming extension is set with the chunk count.
        streaming = rec["extensions"]["io.novafabric.streaming"]
        assert streaming["streamed"] is True
        assert streaming["chunk_count"] == 4

    def test_sse_response_with_no_matching_id_still_creates_record(
        self, tmp_path: Path
    ) -> None:
        """If the SSE stream ends without a response carrying the request's
        id (server bug, connection drop), the record is still emitted with
        an empty response_envelope so the audit trail isn't lost."""
        proxy = MCPHttpProxy(
            listen_host="127.0.0.1", listen_port=0,
            upstream_url="http://upstream/mcp",
            writer=_writer(tmp_path), parent_span_id="0" * 16,
        )
        request = json.dumps({
            "jsonrpc": "2.0", "id": "missing-id-1",
            "method": "tools/call",
            "params": {"name": "x", "arguments": {}},
        }).encode()
        sse_body = self._make_sse_body(
            {"jsonrpc": "2.0", "method": "notifications/progress", "params": {}},
        )

        with patch("httpx.post", return_value=_mock_response(
            body=sse_body, content_type="text/event-stream",
        )):
            proxy.serve_one_request_for_test(request)

        rec = _tool_calls(tmp_path)[0]
        assert rec["mcp"]["response_envelope"] == {}
        assert rec["status"] == "success"  # no 'error' key in empty envelope


# ── Upstream connectivity errors ─────────────────────────────────────────────


class TestUpstreamErrors:
    def test_upstream_unreachable_synthesizes_jsonrpc_error(
        self, tmp_path: Path
    ) -> None:
        import httpx

        proxy = MCPHttpProxy(
            listen_host="127.0.0.1", listen_port=0,
            upstream_url="http://upstream/mcp",
            writer=_writer(tmp_path), parent_span_id="0" * 16,
        )
        request = json.dumps({
            "jsonrpc": "2.0", "id": 7,
            "method": "tools/call",
            "params": {"name": "x", "arguments": {}},
        }).encode()

        with patch("httpx.post", side_effect=httpx.ConnectError("nope")):
            status, body, ctype = proxy.serve_one_request_for_test(request)

        assert status == 502
        assert ctype == "application/json"
        envelope = json.loads(body)
        assert envelope["jsonrpc"] == "2.0"
        assert envelope["id"] == 7
        assert envelope["error"]["code"] == -32000
        assert "unreachable" in envelope["error"]["message"]

        # Even on connectivity failure, the tools/call attempt is recorded.
        rec = _tool_calls(tmp_path)[0]
        assert rec["status"] == "error"


# ── initialize handshake captures server identity ────────────────────────────


class TestInitializeHandshake:
    def test_initialize_response_populates_server_identity(
        self, tmp_path: Path
    ) -> None:
        proxy = MCPHttpProxy(
            listen_host="127.0.0.1", listen_port=0,
            upstream_url="http://upstream/mcp",
            writer=_writer(tmp_path), parent_span_id="0" * 16,
        )
        init_request = json.dumps({
            "jsonrpc": "2.0", "id": "init-1", "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {}},
        }).encode()
        init_response = json.dumps({
            "jsonrpc": "2.0", "id": "init-1",
            "result": {
                "protocolVersion": "2024-11-05",
                "serverInfo": {"name": "my-mcp-server", "version": "1.2.3"},
                "capabilities": {},
            },
        }).encode()
        # Then a tools/call.
        call_request = json.dumps({
            "jsonrpc": "2.0", "id": "c-1",
            "method": "tools/call",
            "params": {"name": "echo", "arguments": {}},
        }).encode()
        call_response = json.dumps({
            "jsonrpc": "2.0", "id": "c-1",
            "result": {"content": []},
        }).encode()

        with patch("httpx.post") as post:
            post.side_effect = [
                _mock_response(body=init_response),
                _mock_response(body=call_response),
            ]
            proxy.serve_one_request_for_test(init_request)
            proxy.serve_one_request_for_test(call_request)

        rec = _tool_calls(tmp_path)[0]
        assert rec["mcp"]["server_name"] == "my-mcp-server"
        assert rec["mcp"]["server_version"] == "1.2.3"
        assert rec["tool_provider"] == "mcp://my-mcp-server"
        assert rec["extensions"]["io.novafabric.mcp_protocol_version"] == "2024-11-05"


# ── Helper functions: SSE parsing ────────────────────────────────────────────


class TestSseAggregator:
    def test_aggregate_picks_response_with_matching_id(self) -> None:
        body = (
            b'data: {"jsonrpc":"2.0","method":"notifications/progress","params":{}}\n\n'
            b'data: {"jsonrpc":"2.0","id":"x","result":{"ok":true}}\n\n'
        )
        msg = _aggregate_sse_for_response(body, "x")
        assert msg["result"]["ok"] is True

    def test_aggregate_returns_empty_when_no_match(self) -> None:
        body = b'data: {"jsonrpc":"2.0","id":"y","result":{}}\n\n'
        assert _aggregate_sse_for_response(body, "x") == {}

    def test_aggregate_handles_trailing_event_without_blank_line(self) -> None:
        body = b'data: {"jsonrpc":"2.0","id":"z","result":{"v":1}}\n'
        msg = _aggregate_sse_for_response(body, "z")
        assert msg["result"]["v"] == 1

    def test_aggregate_skips_malformed_data(self) -> None:
        body = (
            b'data: not-json\n\n'
            b'data: {"jsonrpc":"2.0","id":"w","result":{}}\n\n'
        )
        assert _aggregate_sse_for_response(body, "w") != {}

    def test_count_sse_events_counts_blank_separated_blocks(self) -> None:
        body = (
            b'data: {}\n\n'
            b'data: {}\n\n'
            b'data: {}\n\n'
        )
        assert _count_sse_events(body) == 3

    def test_count_sse_events_counts_trailing_event(self) -> None:
        body = b'data: {}\n\ndata: {}\n'  # second event no trailing blank
        assert _count_sse_events(body) == 2


# ── Server-loop coverage: spin up + immediately shut down ────────────────────


class TestServerLoop:
    """Cover the run() method's setup + clean shutdown path. We do NOT
    serve any actual requests here (those are tested via the
    serve_one_request_for_test seam above). This just proves run()
    binds, accepts the shutdown signal, and exits 0."""

    def test_run_binds_and_shuts_down_cleanly(self, tmp_path: Path) -> None:
        import socket
        import threading
        import time as _time

        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()

        proxy = MCPHttpProxy(
            listen_host="127.0.0.1", listen_port=port,
            upstream_url="http://upstream/mcp",
            writer=_writer(tmp_path), parent_span_id="0" * 16,
        )

        result_holder: dict[str, int] = {}

        def runner_thread() -> None:
            result_holder["exit"] = proxy.run()

        t = threading.Thread(target=runner_thread, daemon=True)
        t.start()
        # Give the server a moment to bind, then shutdown.
        deadline = _time.monotonic() + 2.0
        while proxy._server is None and _time.monotonic() < deadline:
            _time.sleep(0.01)
        assert proxy._server is not None, "server never bound"
        # Trigger clean shutdown from another thread.
        threading.Thread(target=proxy._server.shutdown, daemon=True).start()
        t.join(timeout=2.0)
        assert not t.is_alive(), "run() did not return after shutdown()"
        assert result_holder["exit"] == 0


# ── Edge-case coverage for SSE parser ────────────────────────────────────────


class TestSseEdgeCases:
    def test_aggregate_handles_completely_empty_body(self) -> None:
        assert _aggregate_sse_for_response(b"", "x") == {}

    def test_count_handles_empty_body(self) -> None:
        assert _count_sse_events(b"") == 0

    def test_aggregate_ignores_comment_lines(self) -> None:
        body = (
            b": this is a comment\n"
            b'data: {"jsonrpc":"2.0","id":"x","result":{}}\n\n'
        )
        assert _aggregate_sse_for_response(body, "x") != {}

    def test_aggregate_skips_malformed_trailing_event(self) -> None:
        """Trailing event without blank-line terminator that isn't valid
        JSON should be silently skipped, not crash."""
        body = b'data: not-json-at-all\n'
        assert _aggregate_sse_for_response(body, "anything") == {}

    def test_aggregate_id_match_when_id_is_int(self) -> None:
        """JSON-RPC ids can be numbers; the matcher coerces both sides
        to str so int/string ids align."""
        body = b'data: {"jsonrpc":"2.0","id":42,"result":{}}\n\n'
        assert _aggregate_sse_for_response(body, 42) != {}
        assert _aggregate_sse_for_response(body, "42") != {}


# ── Coverage for tools/call against an upstream that returns malformed JSON ──


class TestMalformedUpstream:
    def test_malformed_json_response_yields_empty_envelope(
        self, tmp_path: Path
    ) -> None:
        """If upstream sends Content-Type: application/json but the body
        isn't parseable, the proxy still records the tools/call attempt
        with an empty response_envelope (audit trail preserved)."""
        proxy = MCPHttpProxy(
            listen_host="127.0.0.1", listen_port=0,
            upstream_url="http://upstream/mcp",
            writer=_writer(tmp_path), parent_span_id="0" * 16,
        )
        request = json.dumps({
            "jsonrpc": "2.0", "id": "m-1",
            "method": "tools/call",
            "params": {"name": "x", "arguments": {}},
        }).encode()

        with patch("httpx.post", return_value=_mock_response(
            body=b"<html>not json at all</html>",
            content_type="application/json",
        )):
            proxy.serve_one_request_for_test(request)

        rec = _tool_calls(tmp_path)[0]
        assert rec["mcp"]["response_envelope"] == {}
