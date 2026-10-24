"""An MCP tool result with ``isError: true`` is recorded as a failed tool call.

Review capture-runners-22: MCP reports a failing tool as ``CallToolResult.isError =
true``, not as a JSON-RPC error. The hook and both proxy builders recorded every such
call as ``status: "success"``, so failure rates, the spool's ``ToolCallCompleted`` and
diff gates counted failed calls as successes, with the truth buried in
``result.isError``. Mocked replay must still return the recorded result — the
original call returned it; it did not raise.
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path
from typing import Any
from unittest.mock import patch

import jsonschema
import pytest

from novafabric.capture.capsule import CapsuleWriter

RUN_ID = "01HXTEST000000000000000000"
TOOL_SCHEMA = json.loads(
    (Path(__file__).resolve().parents[1] / "src" / "novafabric" / "schemas"
     / "tool-call.schema.json").read_text()
)
FAILED = {"content": [{"type": "text", "text": "file not found: x.txt"}], "isError": True}


class _Content:
    type = "text"
    text = "file not found: x.txt"


class _Result:
    content = [_Content()]
    isError = True


class _Session:
    server_name = "filesystem"
    server_version = "1.2.3"

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> _Result:
        return _Result()


def _fake_mcp() -> dict[str, object]:
    session_mod = types.ModuleType("mcp.client.session")
    session_mod.ClientSession = _Session  # type: ignore[attr-defined]
    client_mod = types.ModuleType("mcp.client")
    client_mod.session = session_mod  # type: ignore[attr-defined]
    mcp = types.ModuleType("mcp")
    mcp.client = client_mod  # type: ignore[attr-defined]
    return {"mcp": mcp, "mcp.client": client_mod, "mcp.client.session": session_mod}


def test_the_hook_records_a_tool_reported_error(tmp_path: Path) -> None:
    from novafabric.capture.hooks._mcp import MCPHook

    writer = CapsuleWriter(run_id=RUN_ID, base_dir=tmp_path)
    writer.open()
    hook = MCPHook(writer=writer, parent_span_id="aabbccddeeff0011")
    with patch.dict(sys.modules, _fake_mcp()):
        hook.install()
        try:
            result = asyncio.run(_Session().call_tool("read_file", {"path": "x.txt"}))
        finally:
            hook.uninstall()
    assert result.isError is True  # the caller's result is untouched
    (rec,) = [json.loads(line) for line in (writer.capsule_dir / "tool-calls.jsonl")
              .read_text().splitlines() if line.strip()]
    assert rec["status"] == "error"
    assert rec["error"]["type"] == "McpToolError"
    assert "file not found" in rec["error"]["message"]
    assert rec["result"]["isError"] is True
    jsonschema.validate(rec, TOOL_SCHEMA)


def test_the_http_proxy_records_a_tool_reported_error() -> None:
    from novafabric.proxy.mcp_proxy import _record_from_http_call, _ServerIdentity

    rec = _record_from_http_call(
        request_envelope={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": "read_file", "arguments": {}}},
        response_envelope={"jsonrpc": "2.0", "id": 1, "result": FAILED},
        started_at="2026-10-24T00:00:00Z", finished_at="2026-10-24T00:00:01Z",
        duration_ms=1000, parent_span_id="aabbccddeeff0011",
        server=_ServerIdentity(), streamed=False, chunk_count=0,
    )
    assert rec["status"] == "error" and rec["error"]["type"] == "McpToolError"
    assert rec["result"] == FAILED
    jsonschema.validate(rec, TOOL_SCHEMA)


def _recorded(**overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "transport": "mcp", "status": "error", "result": FAILED,
        "error": {"type": "McpToolError", "message": "file not found: x.txt", "traceback_ref": None},
        "mcp": {"response_envelope": {"jsonrpc": "2.0", "id": 1, "result": FAILED}},
    }
    record.update(overrides)
    return record


def test_replay_returns_a_tool_reported_error_as_the_original_did() -> None:
    from novafabric.replay._dispatcher import _mcp_result_from_record

    result = _mcp_result_from_record(_recorded())
    assert result.isError is True


def test_replay_still_raises_a_json_rpc_error_and_a_recorded_exception() -> None:
    from novafabric.replay._dispatcher import _mcp_result_from_record
    from novafabric.replay._errors import ReplayRecordedToolError

    rpc = _recorded(mcp={"response_envelope": {"jsonrpc": "2.0", "id": 1,
                                                "error": {"code": -32602, "message": "bad"}}})
    with pytest.raises(Exception, match="bad"):
        _mcp_result_from_record(rpc)
    raised = _recorded(
        result=None, mcp={},
        error={"type": "RuntimeError", "message": "server unreachable", "traceback_ref": None},
    )
    with pytest.raises(ReplayRecordedToolError, match="unreachable"):
        _mcp_result_from_record(raised)
