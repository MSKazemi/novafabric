"""Tests for the transparent MCP stdio proxy (ADR-0015 §Secondary).

Pattern: drive :class:`MCPProxy` in-process by passing :class:`io.BytesIO`
streams for ``client_in``/``client_out``. The upstream is a real
``subprocess.Popen`` of a Python one-liner that speaks newline-delimited
JSON-RPC. No third-party MCP server is required.
"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from typing import Any

import jsonschema

from novafabric.capture.capsule import CapsuleWriter
from novafabric.proxy.mcp_proxy import MCPProxy

RUN_ID = "01HXTEST000000000000000000"
SCHEMA = json.loads(
    (Path(__file__).parents[1] / "src/novafabric/schemas/tool-call.schema.json").read_text()
)

# Fake upstream: pure-stdlib JSON-RPC echoer. Behavior matrix:
#   initialize             → reply with serverInfo + protocolVersion
#   tools/call name=broken → reply with JSON-RPC error
#   tools/call any other   → reply with success result
#   anything with no id    → no reply (notification semantics)
_FAKE_SERVER_SCRIPT = r"""
import json, sys
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        msg = json.loads(line)
    except Exception:
        continue
    rpc_id = msg.get("id")
    method = msg.get("method")
    if rpc_id is None:
        # Notification — no response, but we may still emit a server-initiated
        # notification to test passthrough of id-less messages from server side.
        if method == "_emit_notification":
            sys.stdout.write(
                json.dumps({"jsonrpc":"2.0","method":"notifications/cancelled"}) + "\n"
            )
            sys.stdout.flush()
        continue
    if method == "initialize":
        resp = {
            "jsonrpc":"2.0","id":rpc_id,
            "result":{
                "protocolVersion":"2024-11-05",
                "serverInfo":{"name":"fake-fs","version":"9.9.9"},
            },
        }
    elif method == "tools/call":
        params = msg.get("params") or {}
        if params.get("name") == "broken":
            resp = {"jsonrpc":"2.0","id":rpc_id,
                    "error":{"code":-32000,"message":"server unreachable"}}
        else:
            resp = {"jsonrpc":"2.0","id":rpc_id,
                    "result":{"content":[{"type":"text","text":f"ok:{params.get('name')}"}]}}
    elif method == "resources/read":
        resp = {"jsonrpc":"2.0","id":rpc_id,"result":{"contents":[]}}
    else:
        resp = {"jsonrpc":"2.0","id":rpc_id,"error":{"code":-32601,"message":"unknown"}}
    sys.stdout.write(json.dumps(resp) + "\n")
    sys.stdout.flush()
"""


def _upstream_cmd() -> list[str]:
    return [sys.executable, "-c", _FAKE_SERVER_SCRIPT]


def _make_writer(tmp_path: Path) -> CapsuleWriter:
    w = CapsuleWriter(run_id=RUN_ID, base_dir=tmp_path)
    w.open()
    return w


def _tool_calls(tmp_path: Path) -> list[dict[str, Any]]:
    text = (tmp_path / RUN_ID / "tool-calls.jsonl").read_text().strip()
    return [json.loads(line) for line in text.splitlines() if line]


def _client_stream(*messages: dict[str, Any]) -> io.BytesIO:
    """Build a client→proxy stream of newline-delimited JSON-RPC messages."""
    payload = b"".join(
        (json.dumps(m) + "\n").encode("utf-8") for m in messages
    )
    return io.BytesIO(payload)


def _drive(
    tmp_path: Path, *messages: dict[str, Any]
) -> tuple[bytes, list[dict[str, Any]]]:
    """Drive the proxy with the given client messages.

    Returns ``(server_bytes_seen_by_client, recorded_tool_call_records)``.
    """
    writer = _make_writer(tmp_path)
    client_in = _client_stream(*messages)
    client_out = io.BytesIO()
    proxy = MCPProxy(
        upstream_cmd=_upstream_cmd(),
        writer=writer,
        parent_span_id="aabbccddeeff0011",
        client_in=client_in,
        client_out=client_out,
    )
    proxy.run()
    return client_out.getvalue(), _tool_calls(tmp_path)


# ---------------------------------------------------------------------- tests


def test_golden_path_records_one_tool_call(tmp_path: Path) -> None:
    out, records = _drive(
        tmp_path,
        {"jsonrpc": "2.0", "id": "1", "method": "initialize", "params": {}},
        {
            "jsonrpc": "2.0", "id": "2",
            "method": "tools/call",
            "params": {"name": "read_file", "arguments": {"path": "/tmp/x"}},
        },
    )
    assert len(records) == 1
    rec = records[0]
    assert rec["transport"] == "mcp"
    assert rec["tool_name"] == "read_file"
    assert rec["status"] == "success"
    assert rec["mcp"]["method"] == "tools/call"
    assert rec["mcp"]["request_id"] == "2"
    assert rec["mcp"]["envelope"]["params"]["arguments"] == {"path": "/tmp/x"}
    assert rec["mcp"]["response_envelope"]["result"]["content"][0]["text"] == "ok:read_file"
    assert rec["extensions"]["io.novafabric.capture_method"] == "proxy"
    # The client must see both the initialize response and the tools/call response.
    assert b'"id": "1"' in out and b'"id": "2"' in out


def test_jsonrpc_error_response_yields_error_record(tmp_path: Path) -> None:
    _, records = _drive(
        tmp_path,
        {"jsonrpc": "2.0", "id": "init", "method": "initialize", "params": {}},
        {
            "jsonrpc": "2.0", "id": "x",
            "method": "tools/call",
            "params": {"name": "broken", "arguments": {}},
        },
    )
    assert len(records) == 1
    assert records[0]["status"] == "error"
    assert records[0]["error"]["type"] == "JsonRpcError"
    assert "server unreachable" in records[0]["error"]["message"]
    assert records[0]["mcp"]["response_envelope"]["error"]["code"] == -32000


def test_notification_without_id_emits_no_record(tmp_path: Path) -> None:
    """Server-initiated notifications and id-less client messages must pass
    through but never produce a record (they aren't tools/call exchanges)."""
    _, records = _drive(
        tmp_path,
        {"jsonrpc": "2.0", "id": "1", "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "_emit_notification"},  # id-less, no reply
    )
    assert records == []


def test_non_tools_call_method_is_passthrough_only(tmp_path: Path) -> None:
    out, records = _drive(
        tmp_path,
        {"jsonrpc": "2.0", "id": "1", "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": "2", "method": "resources/read", "params": {}},
    )
    assert records == []  # only tools/call is captured in v0.5.x
    assert b'"id": "2"' in out  # but bytes flow back to the client


def test_server_identity_captured_from_initialize(tmp_path: Path) -> None:
    _, records = _drive(
        tmp_path,
        {"jsonrpc": "2.0", "id": "init", "method": "initialize", "params": {}},
        {
            "jsonrpc": "2.0", "id": "1",
            "method": "tools/call",
            "params": {"name": "ping", "arguments": {}},
        },
    )
    assert len(records) == 1
    assert records[0]["mcp"]["server_name"] == "fake-fs"
    assert records[0]["mcp"]["server_version"] == "9.9.9"
    assert records[0]["tool_provider"] == "mcp://fake-fs"
    assert records[0]["extensions"]["io.novafabric.mcp_protocol_version"] == "2024-11-05"


def test_byte_fidelity_request_envelope_preserved(tmp_path: Path) -> None:
    """The recorded envelope must equal the client's parsed message verbatim,
    not a reconstructed/normalized form."""
    args = {"path": "/tmp/x", "deeply": {"nested": [1, 2, {"k": "v"}]}}
    _, records = _drive(
        tmp_path,
        {"jsonrpc": "2.0", "id": "init", "method": "initialize", "params": {}},
        {
            "jsonrpc": "2.0", "id": "42",
            "method": "tools/call",
            "params": {"name": "deep", "arguments": args},
        },
    )
    assert records[0]["mcp"]["envelope"]["params"]["arguments"] == args


def test_record_validates_against_schema(tmp_path: Path) -> None:
    _, records = _drive(
        tmp_path,
        {"jsonrpc": "2.0", "id": "init", "method": "initialize", "params": {}},
        {
            "jsonrpc": "2.0", "id": "1",
            "method": "tools/call",
            "params": {"name": "ping", "arguments": {"n": 3}},
        },
    )
    jsonschema.validate(
        records[0], SCHEMA, format_checker=jsonschema.FormatChecker()
    )


def test_multiple_calls_yield_multiple_records_in_order(tmp_path: Path) -> None:
    _, records = _drive(
        tmp_path,
        {"jsonrpc": "2.0", "id": "init", "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": "1", "method": "tools/call",
         "params": {"name": "a", "arguments": {}}},
        {"jsonrpc": "2.0", "id": "2", "method": "tools/call",
         "params": {"name": "b", "arguments": {}}},
        {"jsonrpc": "2.0", "id": "3", "method": "tools/call",
         "params": {"name": "c", "arguments": {}}},
    )
    assert [r["tool_name"] for r in records] == ["a", "b", "c"]


def test_empty_upstream_cmd_rejected(tmp_path: Path) -> None:
    import pytest
    writer = _make_writer(tmp_path)
    with pytest.raises(ValueError):
        MCPProxy(
            upstream_cmd=[],
            writer=writer,
            parent_span_id="0" * 16,
            client_in=io.BytesIO(),
            client_out=io.BytesIO(),
        )
