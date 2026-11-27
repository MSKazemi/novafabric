"""JSON-RPC / MCP handling of the read-only evidence server (issue #244).

Driven in-process through :class:`EvidenceServer` and :func:`serve` with byte
streams, so every malformed-input path is exercised without a client.
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
from typing import Any

import pytest

from novafabric.mcp import evidence_server
from novafabric.mcp.evidence_server import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    SERVER_NOT_INITIALIZED,
    SUPPORTED_PROTOCOL_VERSIONS,
    TOOLS,
    EvidenceServer,
    serve,
)
from novafabric.mcp.evidence_tools import EvidenceScope

from .conftest import PROMPT, write_capsule


def _rpc(method: str, params: dict[str, Any] | None = None, req_id: object = 1) -> dict[str, Any]:
    message: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        message["params"] = params
    return message


def _server(runs: Path, version: str = "2025-11-25") -> EvidenceServer:
    server = EvidenceServer(EvidenceScope.from_paths([runs]))
    response = server.handle_message(_rpc("initialize", {"protocolVersion": version}))
    assert response is not None and "result" in response
    return server


def _call(server: EvidenceServer, name: str, arguments: object = None) -> dict[str, Any]:
    params: dict[str, Any] = {"name": name}
    if arguments is not None:
        params["arguments"] = arguments
    response = server.handle_message(_rpc("tools/call", params))
    assert response is not None
    return response


# -- lifecycle ---------------------------------------------------------------


@pytest.mark.parametrize("version", SUPPORTED_PROTOCOL_VERSIONS)
def test_initialize_echoes_a_supported_version(runs: Path, version: str) -> None:
    server = EvidenceServer(EvidenceScope.from_paths([runs]))
    result = server.handle_message(_rpc("initialize", {"protocolVersion": version}))["result"]
    assert result["protocolVersion"] == version
    assert result["capabilities"] == {"tools": {"listChanged": False}}
    assert result["serverInfo"]["name"] == "novafabric-evidence"
    assert server.protocol_version == version


@pytest.mark.parametrize("version", ["2099-01-01", "", None, 5, ["2025-06-18"]])
def test_initialize_offers_the_newest_supported_version_otherwise(
    runs: Path, version: object
) -> None:
    server = EvidenceServer(EvidenceScope.from_paths([runs]))
    result = server.handle_message(_rpc("initialize", {"protocolVersion": version}))["result"]
    assert result["protocolVersion"] == SUPPORTED_PROTOCOL_VERSIONS[0]


def test_tools_need_initialize_first_but_ping_does_not(runs: Path) -> None:
    server = EvidenceServer(EvidenceScope.from_paths([runs]))
    assert server.handle_message(_rpc("ping")) == {"jsonrpc": "2.0", "id": 1, "result": {}}
    for method in ("tools/list", "tools/call"):
        response = server.handle_message(_rpc(method, {"name": "list_capsules"}))
        assert response["error"]["code"] == SERVER_NOT_INITIALIZED


def test_tools_list_declares_three_read_only_closed_world_tools(runs: Path) -> None:
    result = _server(runs).handle_message(_rpc("tools/list"))["result"]
    assert [t["name"] for t in result["tools"]] == [
        "list_capsules",
        "get_capsule_summary",
        "verify_capsule",
    ]
    for tool in result["tools"]:
        assert tool["annotations"]["readOnlyHint"] is True
        assert tool["annotations"]["destructiveHint"] is False
        assert tool["annotations"]["openWorldHint"] is False
        assert tool["inputSchema"]["type"] == "object"
        assert tool["inputSchema"]["additionalProperties"] is False
    assert result["tools"] is TOOLS


# -- malformed JSON-RPC ------------------------------------------------------


@pytest.mark.parametrize(
    ("message", "code"),
    [
        ([_rpc("ping")], INVALID_REQUEST),  # batches were removed in MCP 2025-06-18
        ("ping", INVALID_REQUEST),
        ({"id": 1, "method": "ping"}, INVALID_REQUEST),  # no jsonrpc member
        ({"jsonrpc": "1.0", "id": 1, "method": "ping"}, INVALID_REQUEST),
        ({"jsonrpc": "2.0", "id": 1}, INVALID_REQUEST),  # no method, not a response
        ({"jsonrpc": "2.0", "id": None, "method": "ping"}, INVALID_REQUEST),
        ({"jsonrpc": "2.0", "id": True, "method": "ping"}, INVALID_REQUEST),
        ({"jsonrpc": "2.0", "id": 1.5, "method": "ping"}, INVALID_REQUEST),
        ({"jsonrpc": "2.0", "id": 1, "method": "ping", "params": [1]}, INVALID_PARAMS),
        (_rpc("resources/read", {"uri": "file:///etc/passwd"}), METHOD_NOT_FOUND),
    ],
)
def test_malformed_messages_get_a_json_rpc_error(runs: Path, message: object, code: int) -> None:
    response = _server(runs).handle_message(message)
    assert response is not None
    assert response["error"]["code"] == code


def test_invalid_json_is_a_parse_error(runs: Path) -> None:
    server = _server(runs)
    for raw in (b"{not json", b"\xff\xfe", b""):
        assert server.handle_line(raw)["error"]["code"] == PARSE_ERROR


def test_notifications_and_client_responses_get_no_answer(runs: Path) -> None:
    server = _server(runs)
    assert server.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert server.handle_message({"jsonrpc": "2.0", "method": "tools/call"}) is None
    assert server.handle_message({"jsonrpc": "2.0", "id": 9, "result": {}}) is None


def test_an_unexpected_failure_is_an_internal_error_not_a_crash(
    monkeypatch: pytest.MonkeyPatch, runs: Path
) -> None:
    server = _server(runs)

    def boom(*_: object) -> None:
        raise RuntimeError(f"/very/secret/path {PROMPT}")

    monkeypatch.setattr(server, "_dispatch", boom)
    response = server.handle_message(_rpc("ping"))
    assert response["error"]["code"] == INTERNAL_ERROR
    assert PROMPT not in json.dumps(response)


# -- tools/call --------------------------------------------------------------


def test_unknown_tool_and_bad_arguments_are_protocol_errors(runs: Path) -> None:
    server = _server(runs)
    assert _call(server, "delete_capsule", {})["error"]["code"] == INVALID_PARAMS
    assert _call(server, "verify_capsule", ["r1"])["error"]["code"] == INVALID_PARAMS
    response = server.handle_message(_rpc("tools/call", {"name": 7}))
    assert response["error"]["code"] == INVALID_PARAMS


def test_unknown_arguments_are_a_tool_error(runs: Path) -> None:
    result = _call(_server(runs), "verify_capsule", {"capsule": "r1", "path": "/etc"})["result"]
    assert result["isError"] is True
    assert result["structuredContent"]["error"]["code"] == "invalid_argument"


def test_a_scope_escape_is_a_tool_error_with_a_code(runs: Path) -> None:
    write_capsule(runs, "r1")
    result = _call(_server(runs), "get_capsule_summary", {"capsule": "../r1"})["result"]
    assert result["isError"] is True
    error = json.loads(result["content"][0]["text"])["error"]
    assert error["code"] == "invalid_argument"
    assert result["structuredContent"]["error"] == error


def test_a_successful_call_carries_text_and_structured_content(runs: Path) -> None:
    write_capsule(runs, "r1")
    result = _call(_server(runs), "list_capsules")["result"]
    assert result["isError"] is False
    payload = json.loads(result["content"][0]["text"])
    assert payload == result["structuredContent"]
    assert payload["capsules"] == [{"root": 0, "capsule": "r1", "sealed": False}]


def test_structured_content_only_from_2025_06_18(runs: Path) -> None:
    write_capsule(runs, "r1")
    result = _call(_server(runs, "2025-03-26"), "list_capsules")["result"]
    assert "structuredContent" not in result
    assert json.loads(result["content"][0]["text"])["count"] == 1


def test_a_tool_failure_fails_closed(monkeypatch: pytest.MonkeyPatch, runs: Path) -> None:
    write_capsule(runs, "r1")
    server = _server(runs)

    def boom(*_: object) -> None:
        raise OSError("disk on fire")

    monkeypatch.setattr(EvidenceScope, "verify_capsule", boom)
    result = _call(server, "verify_capsule", {"capsule": "r1"})["result"]
    assert result["isError"] is True
    assert result["structuredContent"]["error"]["code"] == "internal_error"
    assert "valid" not in result["structuredContent"]
    assert "disk on fire" not in result["content"][0]["text"]


def test_an_oversized_result_is_refused_not_truncated(
    monkeypatch: pytest.MonkeyPatch, runs: Path
) -> None:
    for i in range(20):
        write_capsule(runs, f"run-{i:02d}")
    monkeypatch.setattr(evidence_server, "MAX_RESPONSE_BYTES", 200)
    result = _call(_server(runs), "list_capsules", {"limit": 20})["result"]
    assert result["isError"] is True
    assert result["structuredContent"]["error"]["code"] == "response_too_large"


# -- the stdio loop ----------------------------------------------------------


def _run(runs: Path, data: bytes) -> list[dict[str, Any]]:
    out = io.BytesIO()
    assert serve(EvidenceScope.from_paths([runs]), io.BytesIO(data), out) == 0
    return [json.loads(line) for line in out.getvalue().splitlines()]


def test_serve_answers_line_by_line_and_skips_blank_lines(runs: Path) -> None:
    lines = [
        json.dumps(_rpc("initialize", {"protocolVersion": "2025-11-25"}, req_id="a")),
        "",
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        json.dumps(_rpc("tools/list", req_id=2)),
    ]
    responses = _run(runs, ("\n".join(lines) + "\n").encode())
    assert [r["id"] for r in responses] == ["a", 2]


def test_serve_refuses_an_oversized_message_and_keeps_going(
    monkeypatch: pytest.MonkeyPatch, runs: Path
) -> None:
    monkeypatch.setattr(evidence_server, "MAX_MESSAGE_BYTES", 64)
    huge = json.dumps(_rpc("ping", {"pad": "x" * 500}, req_id=1)).encode()
    data = huge + b"\n" + json.dumps(_rpc("ping", req_id=2)).encode() + b"\n"
    responses = _run(runs, data)
    assert responses[0]["error"]["code"] == INVALID_REQUEST
    assert "larger than" in responses[0]["error"]["message"]
    assert responses[1] == {"jsonrpc": "2.0", "id": 2, "result": {}}


def test_serve_handles_a_final_line_without_newline(runs: Path) -> None:
    assert _run(runs, json.dumps(_rpc("ping")).encode()) == [
        {"jsonrpc": "2.0", "id": 1, "result": {}}
    ]


def test_main_refuses_a_bad_scope(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert evidence_server.main(["--scope", str(tmp_path / "missing")]) == 2
    assert "not a directory" in capsys.readouterr().err


def test_main_requires_a_scope(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as info:
        evidence_server.main([])
    assert info.value.code == 2
    assert "--scope" in capsys.readouterr().err


# -- inputs that once ended the process (independent review of #346) --------


def _non_utf8_capsule(runs: Path) -> None:
    """A capsule whose directory name is not valid UTF-8 (``caf\\xe9``)."""
    name = os.fsencode(runs) + b"/caf\xe9"
    try:
        os.mkdir(name)
    except OSError:  # pragma: no cover - a filesystem that only takes UTF-8 names
        pytest.skip("this filesystem refuses non-UTF-8 file names")
    with open(name + b"/capsule.yaml", "wb") as fh:
        fh.write(b"run_id: cafe\n")


def test_a_lone_surrogate_in_the_method_is_answered_and_the_session_continues(
    runs: Path,
) -> None:
    init = json.dumps(_rpc("initialize", {"protocolVersion": "2025-11-25"}, req_id=0))
    bad = '{"jsonrpc":"2.0","id":1,"method":"tools/\\udcff"}'
    data = (init + "\n" + bad + "\n" + json.dumps(_rpc("ping", req_id=2)) + "\n").encode()
    out = io.BytesIO()
    assert serve(EvidenceScope.from_paths([runs]), io.BytesIO(data), out) == 0
    out.getvalue().decode("ascii")  # every response line is plain ASCII JSON
    responses = [json.loads(line) for line in out.getvalue().splitlines()]
    assert responses[1]["id"] == 1
    assert responses[1]["error"]["code"] == METHOD_NOT_FOUND
    assert responses[2] == {"jsonrpc": "2.0", "id": 2, "result": {}}


def test_deeply_nested_json_is_a_parse_error_and_the_session_continues(runs: Path) -> None:
    deep = b"[" * 200_000 + b"]" * 200_000
    data = deep + b"\n" + json.dumps(_rpc("ping", req_id=2)).encode() + b"\n"
    responses = _run(runs, data)
    assert responses[0]["error"]["code"] == PARSE_ERROR
    assert responses[1] == {"jsonrpc": "2.0", "id": 2, "result": {}}


def test_a_non_utf8_capsule_name_is_skipped_and_counted_not_a_failure(runs: Path) -> None:
    write_capsule(runs, "good")
    _non_utf8_capsule(runs)
    server = _server(runs)
    result = _call(server, "list_capsules")["result"]
    assert result["isError"] is False
    payload = result["structuredContent"]
    assert [c["capsule"] for c in payload["capsules"]] == ["good"]
    assert payload["skipped_unaddressable"] == 1
    assert _call(server, "verify_capsule", {"capsule": "good"})["result"]["isError"] is False


def test_an_unexpected_failure_while_handling_a_line_keeps_the_session(
    monkeypatch: pytest.MonkeyPatch, runs: Path
) -> None:
    calls = {"n": 0}
    original = EvidenceServer.handle_line

    def flaky(self: EvidenceServer, raw: bytes) -> dict[str, Any] | None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated")
        return original(self, raw)

    monkeypatch.setattr(EvidenceServer, "handle_line", flaky)
    data = json.dumps(_rpc("ping", req_id=1)) + "\n" + json.dumps(_rpc("ping", req_id=2)) + "\n"
    responses = _run(runs, data.encode())
    assert responses[0]["error"]["code"] == INTERNAL_ERROR
    assert responses[1] == {"jsonrpc": "2.0", "id": 2, "result": {}}
