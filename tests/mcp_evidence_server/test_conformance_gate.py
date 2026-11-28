"""Conformance gate for the read-only MCP evidence server (issue #245).

One file pins the contract an MCP client and an auditor rely on, in four parts:

* **protocol** — every response is one ASCII JSON-RPC 2.0 line carrying the
  request's id and exactly one of ``result`` / ``error``; the error-code table;
  the server advertises exactly what it implements (capabilities, tools, input
  schemas) and the ``nova serve`` Server Card does not advertise it at all;
* **privacy** — prompt and response content, tool arguments, command-line
  secrets, host names, environment values and absolute paths appear in no
  result, no error and no log line, on every tool, every refusal and every
  supported protocol revision;
* **adversarial capsule data** — traversal names, links out of scope, FIFOs,
  over-size, malformed and deeply nested manifests, YAML alias amplification,
  secrets and scope paths straddling the string cap, and every seal failure
  (missing, empty, bad signature, corrupted, partial provenance) each get an
  accurate refusal or ``valid: false`` — never a hang, a crash or a "valid";
* **offline** — a whole session with the network and subprocesses disabled.

It runs in the ordinary unit job: no network, no LLM, no API key, fixed
fixtures built in ``tmp_path``.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import shutil
import socket
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from novafabric import _capsule_manifest
from novafabric.mcp import evidence_tools
from novafabric.mcp.evidence_server import (
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    SERVER_NAME,
    SERVER_NOT_INITIALIZED,
    SUPPORTED_PROTOCOL_VERSIONS,
    TOOLS,
    serve,
)
from novafabric.mcp.evidence_tools import EvidenceScope
from novafabric.mcp.servercard import build_server_card

from .conftest import PROMPT, SECRET, seal, write_capsule

#: A tool argument recorded in the capsule; it must never come back.
TOOL_ARGUMENT = "TOOL-ARGUMENT-NEVER-RETURNED"
#: A value placed in the server process's environment.
ENV_CANARY = "ENV-CANARY-NEVER-RETURNED"
#: Content of a file outside every scope, reachable only through a link.
OUTSIDE = "OUTSIDE-SCOPE-NEVER-RETURNED"

_TOOL_NAMES = {"list_capsules", "get_capsule_summary", "verify_capsule"}


# -- session helpers ---------------------------------------------------------


def _initialize(version: str = "2025-11-25") -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": "init",
        "method": "initialize",
        "params": {
            "protocolVersion": version,
            "capabilities": {},
            "clientInfo": {"name": "conformance-gate", "version": "0"},
        },
    }


_INITIALIZED = {"jsonrpc": "2.0", "method": "notifications/initialized"}


def _request(req_id: object, method: str, params: object = None) -> dict[str, Any]:
    message: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        message["params"] = params
    return message


def _tool_call(req_id: object, name: str, arguments: object = None) -> dict[str, Any]:
    params: dict[str, Any] = {"name": name}
    if arguments is not None:
        params["arguments"] = arguments
    return _request(req_id, "tools/call", params)


def _encode(message: object) -> bytes:
    return message if isinstance(message, bytes) else json.dumps(message).encode()


def _assert_envelope(response: dict[str, Any]) -> None:
    """The JSON-RPC 2.0 response shape every line must have."""
    assert response["jsonrpc"] == "2.0"
    assert "id" in response
    assert set(response) <= {"jsonrpc", "id", "result", "error"}
    assert ("result" in response) != ("error" in response)
    if "error" in response:
        error = response["error"]
        assert set(error) <= {"code", "message", "data"}
        assert isinstance(error["code"], int) and not isinstance(error["code"], bool)
        assert isinstance(error["message"], str) and error["message"]


def _session(
    roots: list[Path],
    messages: list[object],
    *,
    version: str | None = "2025-11-25",
) -> tuple[list[dict[str, Any]], str]:
    """Run *messages* through :func:`serve` as newline-delimited JSON-RPC.

    With a *version*, an ``initialize`` request and the ``initialized``
    notification go first and the ``initialize`` response is checked and
    dropped. Returns the remaining responses and the raw stdout text.
    """
    preamble: list[object] = [_initialize(version), _INITIALIZED] if version else []
    data = b"".join(_encode(m) + b"\n" for m in [*preamble, *messages])
    out = io.BytesIO()
    assert serve(EvidenceScope.from_paths(roots), io.BytesIO(data), out) == 0
    raw = out.getvalue()
    assert raw == b"" or raw.endswith(b"\n")
    text = raw.decode("ascii")  # ASCII-only JSON, one message per line
    responses = [json.loads(line) for line in text.splitlines()]
    for response in responses:
        _assert_envelope(response)
    if version:
        init = responses.pop(0)
        assert init["id"] == "init"
        assert init["result"]["protocolVersion"] == version
    return responses, text


def _tool_payload(response: dict[str, Any], version: str = "2025-11-25") -> tuple[bool, Any]:
    """``(isError, payload)`` of a ``tools/call`` result, checking its shape."""
    result = response["result"]
    assert isinstance(result["isError"], bool)
    assert len(result["content"]) == 1
    item = result["content"][0]
    assert item["type"] == "text"
    payload = json.loads(item["text"])
    if version >= "2025-06-18":
        assert result["structuredContent"] == payload
    else:
        assert "structuredContent" not in result
    if result["isError"]:
        assert set(payload) == {"error"}
        assert set(payload["error"]) == {"code", "message"}
    return result["isError"], payload


# -- fixtures ----------------------------------------------------------------


@pytest.fixture
def seal_work(tmp_path: Path) -> Path:
    return tmp_path / "seal-work"


@pytest.fixture
def make_sealed(seal_work: Path) -> Callable[[Path, str], Path]:
    def _make(root: Path, name: str) -> Path:
        capsule = write_capsule(root, name)
        seal(capsule, seal_work)
        return capsule

    return _make


@pytest.fixture
def hostile(
    tmp_path: Path,
    runs: Path,
    make_sealed: Callable[[Path, str], Path],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[list[Path]]:
    """Two scope directories full of sensitive material, plus links out of scope."""
    monkeypatch.setenv("NOVA_CONFORMANCE_CANARY", ENV_CANARY)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text(OUTSIDE)
    outside_capsule = write_capsule(outside, "elsewhere", run_id=OUTSIDE)

    write_capsule(
        runs,
        "plain",
        alias=f"token {SECRET}",
        environment={"OPENAI_API_KEY": SECRET, "NOVA_CONFORMANCE_CANARY": ENV_CANARY},
        metadata={"note": PROMPT},
        inputs=[str(outside / "secret.txt")],
        error=f"Traceback: {PROMPT} at {outside}",
    )
    with (runs / "plain" / "tool-calls.jsonl").open("w") as fh:
        fh.write(json.dumps({"tool": "t", "arguments": {"q": TOOL_ARGUMENT, "k": SECRET}}) + "\n")
    make_sealed(runs, "sealed")
    tampered = make_sealed(runs, "tampered")
    with (tampered / "model-calls.jsonl").open("a") as fh:
        fh.write(json.dumps({"messages": [{"content": PROMPT}]}) + "\n")
    (runs / "linked").symlink_to(outside_capsule)
    (runs / "not-a-capsule").mkdir()
    (runs / "not-a-capsule" / "notes.txt").write_text(PROMPT)

    second = tmp_path / "second"
    second.mkdir()
    write_capsule(second, "plain")  # same name as in the first scope: ambiguous
    yield [runs, second]


def _canaries(tmp_path: Path) -> list[str]:
    return [
        PROMPT,
        SECRET,
        TOOL_ARGUMENT,
        ENV_CANARY,
        OUTSIDE,
        "build-host-7",
        "agent.py",
        str(tmp_path),
        os.path.realpath(tmp_path),
    ]


def _assert_no_canary(text: str, tmp_path: Path) -> None:
    for canary in _canaries(tmp_path):
        assert canary not in text, f"leaked: {canary!r}"


# -- protocol ----------------------------------------------------------------


def test_every_response_is_one_well_formed_line_echoing_its_request_id(runs: Path) -> None:
    write_capsule(runs, "c1")
    big_id = 2**53 + 1
    responses, _ = _session(
        [runs],
        [
            _request(1, "ping"),
            _request("two", "tools/list"),
            _INITIALIZED,  # a notification: no answer
            _tool_call(3, "list_capsules"),
            _tool_call(4, "get_capsule_summary", {"capsule": "c1"}),
            _tool_call(5, "verify_capsule", {"capsule": "c1"}),
            _request(big_id, "no/such/method"),
            {"jsonrpc": "2.0", "id": 99, "result": {}},  # a client response: no answer
        ],
    )
    assert [r["id"] for r in responses] == [1, "two", 3, 4, 5, big_id]
    assert responses[0]["result"] == {}
    assert responses[-1]["error"]["code"] == METHOD_NOT_FOUND
    for response in responses[2:5]:
        _tool_payload(response)


@pytest.mark.parametrize(
    ("line", "code", "echoed_id"),
    [
        (b"{not json", PARSE_ERROR, None),
        (b"\xff\xfe\x00", PARSE_ERROR, None),
        (b'[{"jsonrpc":"2.0","id":1,"method":"ping"}]', INVALID_REQUEST, None),
        (b'"ping"', INVALID_REQUEST, None),
        (b'{"jsonrpc":"2.0","id":{"a":1},"method":"ping"}', INVALID_REQUEST, None),
        (b'{"jsonrpc":"2.0","id":[1],"method":"ping"}', INVALID_REQUEST, None),
        (b'{"jsonrpc":"2.0","id":7,"method":"tools/list","params":"x"}', INVALID_PARAMS, 7),
        (b'{"jsonrpc":"2.0","id":7,"method":"tools/list","params":[1]}', INVALID_PARAMS, 7),
        (b'{"jsonrpc":"2.0","id":7,"method":"tools/call","params":{}}', INVALID_PARAMS, 7),
        (
            b'{"jsonrpc":"2.0","id":7,"method":"tools/call",'
            b'"params":{"name":"list_capsules","arguments":[1]}}',
            INVALID_PARAMS,
            7,
        ),
        # No replay, diff, export or write tool exists: asking for one is refused,
        # never approximated by another tool.
        (
            b'{"jsonrpc":"2.0","id":7,"method":"tools/call","params":{"name":"replay_capsule"}}',
            INVALID_PARAMS,
            7,
        ),
        (
            b'{"jsonrpc":"2.0","id":7,"method":"tools/call","params":{"name":"delete_capsule"}}',
            INVALID_PARAMS,
            7,
        ),
    ],
)
def test_the_json_rpc_error_code_table(
    runs: Path, line: bytes, code: int, echoed_id: object
) -> None:
    responses, _ = _session([runs], [line, _request("after", "ping")])
    assert responses[0]["error"]["code"] == code
    assert responses[0]["id"] == echoed_id
    assert responses[1] == {"jsonrpc": "2.0", "id": "after", "result": {}}  # session goes on


def test_only_initialize_and_ping_are_answered_before_initialize(runs: Path) -> None:
    write_capsule(runs, "c1")
    responses, _ = _session(
        [runs],
        [
            _request(1, "ping"),
            _request(2, "tools/list"),
            _tool_call(3, "get_capsule_summary", {"capsule": "c1"}),
            _initialize(),
            _request(4, "tools/list"),
        ],
        version=None,
    )
    assert responses[0]["result"] == {}
    assert responses[1]["error"]["code"] == SERVER_NOT_INITIALIZED
    assert responses[2]["error"]["code"] == SERVER_NOT_INITIALIZED
    assert "result" in responses[3]
    assert [t["name"] for t in responses[4]["result"]["tools"]] == [t["name"] for t in TOOLS]


@pytest.mark.parametrize(
    "method",
    [
        "resources/list",
        "resources/read",
        "resources/templates/list",
        "resources/subscribe",
        "prompts/list",
        "prompts/get",
        "logging/setLevel",
        "completion/complete",
        "sampling/createMessage",
        "elicitation/create",
        "tasks/get",
        "roots/list",
    ],
)
def test_an_unadvertised_capability_is_method_not_found(runs: Path, method: str) -> None:
    responses, _ = _session([runs], [_request(1, method, {"uri": "file:///etc/passwd"})])
    assert responses[0]["error"]["code"] == METHOD_NOT_FOUND


def test_the_server_advertises_exactly_what_it_implements(runs: Path) -> None:
    write_capsule(runs, "c1")
    out = io.BytesIO()
    data = json.dumps(_initialize()).encode() + b"\n"
    serve(EvidenceScope.from_paths([runs]), io.BytesIO(data), out)
    init = json.loads(out.getvalue())["result"]
    assert init["capabilities"] == {"tools": {"listChanged": False}}
    assert init["serverInfo"]["name"] == SERVER_NAME

    assert {tool["name"] for tool in TOOLS} == _TOOL_NAMES
    for tool in TOOLS:
        assert tool["description"] and tool["title"]
        assert tool["inputSchema"]["type"] == "object"
        assert tool["inputSchema"]["additionalProperties"] is False
        assert tool["annotations"]["readOnlyHint"] is True
        assert tool["annotations"]["destructiveHint"] is False
        assert tool["annotations"]["openWorldHint"] is False

    # Every declared argument is accepted; an undeclared one and a missing
    # required one are refused as invalid_argument.
    valid = {"root": 0, "cursor": "0/a", "limit": 1, "capsule": "c1"}
    messages: list[object] = []
    for index, tool in enumerate(TOOLS):
        declared = tool["inputSchema"]["properties"]
        messages.append(_tool_call(index * 10, tool["name"], {k: valid[k] for k in declared}))
        messages.append(_tool_call(index * 10 + 1, tool["name"], {"path": "/etc/passwd"}))
        if tool["inputSchema"].get("required"):
            messages.append(_tool_call(index * 10 + 2, tool["name"], {}))
    responses, _ = _session([runs], messages)
    by_id = {r["id"]: r for r in responses}
    for index, tool in enumerate(TOOLS):
        is_error, payload = _tool_payload(by_id[index * 10])
        assert is_error is False, (tool["name"], payload)
        is_error, payload = _tool_payload(by_id[index * 10 + 1])
        assert is_error is True
        assert payload["error"]["code"] == "invalid_argument"
        if tool["inputSchema"].get("required"):
            is_error, payload = _tool_payload(by_id[index * 10 + 2])
            assert is_error is True
            assert payload["error"]["code"] == "invalid_argument"


def test_the_server_card_does_not_advertise_the_stdio_evidence_server() -> None:
    """The ``nova serve`` card describes the HTTP capture surface only."""
    card = build_server_card()
    text = card.model_dump_json()
    assert card.endpoints == []  # no endpoint without an explicit --base-url
    assert SERVER_NAME not in text
    for name in _TOOL_NAMES:
        assert name not in text
    assert "stdio" not in text
    for capability in ("resources", "prompts", "logging", "completions"):
        assert capability not in card.capabilities


# -- privacy -----------------------------------------------------------------


@pytest.mark.parametrize("version", SUPPORTED_PROTOCOL_VERSIONS)
def test_no_result_or_refusal_leaks_content_secrets_env_or_paths(
    tmp_path: Path, hostile: list[Path], version: str
) -> None:
    messages: list[object] = [
        _tool_call(1, "list_capsules"),
        _tool_call(2, "list_capsules", {"root": 1, "limit": 1}),
        _tool_call(3, "get_capsule_summary", {"capsule": "plain"}),  # ambiguous_capsule
    ]
    names = ["plain", "sealed", "tampered", "linked", "not-a-capsule", "missing", "../outside"]
    req = 10
    for name in names:
        for tool in ("get_capsule_summary", "verify_capsule"):
            messages.append(_tool_call(req, tool, {"capsule": name, "root": 0}))
            req += 1
    messages.append(_tool_call(req, "verify_capsule", {"capsule": "plain", "secret": SECRET}))
    responses, text = _session(hostile, messages, version=version)
    assert len(responses) == len(messages)

    codes = set()
    for response in responses:
        is_error, payload = _tool_payload(response, version)
        if is_error:
            codes.add(payload["error"]["code"])
    # Every refusal kind a client can provoke was exercised here.
    assert codes >= {
        "ambiguous_capsule",
        "outside_scope",
        "capsule_not_found",
        "invalid_argument",
    }
    _assert_no_canary(text, tmp_path)
    _assert_no_canary(json.dumps(responses, ensure_ascii=False), tmp_path)

    summary = json.loads(responses[3]["result"]["content"][0]["text"])
    assert summary["fields"]["run_id"] == "plain"
    assert "[REDACTED:" in summary["fields"]["alias"]
    assert {"command", "environment", "metadata", "inputs", "error", "host"} <= set(
        summary["withheld_fields"]
    )


def test_an_unexpected_failure_leaks_nothing_into_the_result_or_the_log(
    tmp_path: Path,
    runs: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An MCP host commonly keeps the server's stderr; the log carries no data either."""
    write_capsule(runs, "c1")

    def boom(*_: object) -> None:
        raise RuntimeError(f"{SECRET} {PROMPT} {runs / 'c1'}")

    monkeypatch.setattr(EvidenceScope, "get_capsule_summary", boom)
    monkeypatch.setattr(EvidenceScope, "list_capsules", boom)
    with caplog.at_level(logging.DEBUG, logger="novafabric.mcp"):
        responses, text = _session(
            [runs],
            [
                _tool_call(1, "get_capsule_summary", {"capsule": "c1"}),
                _tool_call(2, "list_capsules"),
            ],
        )
    for response in responses:
        is_error, payload = _tool_payload(response)
        assert is_error is True
        assert payload["error"]["code"] == "internal_error"
        assert "RuntimeError" in payload["error"]["message"]
    _assert_no_canary(text, tmp_path)
    assert "RuntimeError" in caplog.text
    assert "get_capsule_summary" in caplog.text  # the tool and the location are named
    _assert_no_canary(caplog.text, tmp_path)
    for record in caplog.records:
        assert record.exc_info is None  # no traceback text carrying the message


def test_a_request_handler_failure_logs_no_message_either(
    tmp_path: Path,
    runs: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from novafabric.mcp.evidence_server import EvidenceServer

    def boom(*_: object) -> None:
        raise ValueError(f"{SECRET} {PROMPT} {tmp_path}")

    monkeypatch.setattr(EvidenceServer, "_dispatch", boom)
    with caplog.at_level(logging.DEBUG, logger="novafabric.mcp"):
        responses, text = _session([runs], [_request(1, "ping")], version=None)
    assert responses[0]["error"]["code"] == -32603
    assert "ValueError" in caplog.text
    _assert_no_canary(text, tmp_path)
    _assert_no_canary(caplog.text, tmp_path)


# -- adversarial: names and links --------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "..",
        ".",
        "",
        "../outside",
        "../outside/elsewhere",
        "..\\outside",
        "/etc",
        "/etc/passwd",
        "a/b",
        "x\x00y",
        "‮evil",
        "​",
        "\n",
        "a" * 300,
        "%2e%2e%2foutside",
        "~",
    ],
)
def test_a_traversal_name_never_reads_outside_the_scope(
    tmp_path: Path, runs: Path, name: str
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    write_capsule(outside, "elsewhere", run_id=OUTSIDE)
    responses, text = _session(
        [runs],
        [
            _tool_call(1, "get_capsule_summary", {"capsule": name}),
            _tool_call(2, "verify_capsule", {"capsule": name}),
        ],
    )
    for response in responses:
        is_error, payload = _tool_payload(response)
        assert is_error is True
        assert payload["error"]["code"] in {"invalid_argument", "capsule_not_found"}
    assert OUTSIDE not in text


def test_links_out_of_scope_are_never_followed(
    tmp_path: Path, runs: Path, make_sealed: Callable[[Path, str], Path]
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "manifest.yaml").write_text(f"run_id: {OUTSIDE}\nstatus: {OUTSIDE}\n")
    donor = make_sealed(outside, "donor")
    elsewhere = write_capsule(outside, "elsewhere", run_id=OUTSIDE)

    (runs / "entry-link").symlink_to(elsewhere)
    manifest_link = runs / "manifest-link"
    manifest_link.mkdir()
    (manifest_link / "capsule.json").write_text('{"run_id": "manifest-link"}')
    (manifest_link / "capsule.yaml").symlink_to(outside / "manifest.yaml")
    envelope_link = make_sealed(runs, "envelope-link")
    (envelope_link / ".seal" / "manifest.dsse").unlink()
    (envelope_link / ".seal" / "manifest.dsse").symlink_to(donor / ".seal" / "manifest.dsse")

    responses, text = _session(
        [runs],
        [
            _tool_call(1, "list_capsules"),
            _tool_call(2, "get_capsule_summary", {"capsule": "entry-link"}),
            _tool_call(3, "verify_capsule", {"capsule": "entry-link"}),
            _tool_call(4, "get_capsule_summary", {"capsule": "manifest-link"}),
            _tool_call(5, "get_capsule_summary", {"capsule": "envelope-link"}),
            _tool_call(6, "verify_capsule", {"capsule": "envelope-link"}),
        ],
    )
    assert OUTSIDE not in text
    _, listed = _tool_payload(responses[0])
    assert "entry-link" not in [c["capsule"] for c in listed["capsules"]]
    for index in (1, 2):
        is_error, payload = _tool_payload(responses[index])
        assert (is_error, payload["error"]["code"]) == (True, "outside_scope")
    for index in (3, 4):
        is_error, payload = _tool_payload(responses[index])
        assert (is_error, payload["error"]["code"]) == (True, "capsule_unreadable")
    is_error, report = _tool_payload(responses[5])
    assert is_error is False
    assert report["valid"] is False
    assert report["signature_ok"] is False
    assert any("symlink" in error for error in report["errors"])


# -- adversarial: special files, size and malformed data ---------------------


@pytest.mark.timeout(60)
@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs FIFOs")
def test_a_planted_fifo_is_refused_without_blocking(
    runs: Path, make_sealed: Callable[[Path, str], Path]
) -> None:
    """Opening a FIFO for reading blocks until a writer comes; nothing here may wait."""
    manifest_fifo = runs / "manifest-fifo"
    manifest_fifo.mkdir()
    (manifest_fifo / "capsule.json").write_text('{"run_id": "manifest-fifo"}')
    os.mkfifo(manifest_fifo / "capsule.yaml")
    evidence_fifo = make_sealed(runs, "evidence-fifo")
    (evidence_fifo / "model-calls.jsonl").unlink()
    os.mkfifo(evidence_fifo / "model-calls.jsonl")
    envelope_fifo = make_sealed(runs, "envelope-fifo")
    (envelope_fifo / ".seal" / "manifest.dsse").unlink()
    os.mkfifo(envelope_fifo / ".seal" / "manifest.dsse")

    responses, _ = _session(
        [runs],
        [
            _tool_call(1, "get_capsule_summary", {"capsule": "manifest-fifo"}),
            _tool_call(2, "verify_capsule", {"capsule": "evidence-fifo"}),
            _tool_call(3, "get_capsule_summary", {"capsule": "envelope-fifo"}),
            _tool_call(4, "verify_capsule", {"capsule": "envelope-fifo"}),
        ],
    )
    for index in (0, 2):
        is_error, payload = _tool_payload(responses[index])
        assert (is_error, payload["error"]["code"]) == (True, "capsule_unreadable")
    for index in (1, 3):
        is_error, report = _tool_payload(responses[index])
        assert is_error is False
        assert report["valid"] is False
        assert report["errors"]


@pytest.mark.parametrize(
    ("filename", "content"),
    [
        ("capsule.yaml", b"run_id: [unclosed\n"),
        ("capsule.yaml", b"- a list\n- not a mapping\n"),
        ("capsule.yaml", b""),
        ("capsule.yaml", b"\xff\xfe\x00 not utf-8"),
        ("capsule.yaml", b"a: " + b"[" * 10_000 + b"]" * 10_000 + b"\n"),
        ("capsule.json", b"[]"),
        ("capsule.json", b'{"run_id": "x", '),
        ("capsule.json", b'{"run_id": ' + b"1" * 5000 + b"}"),
        ("capsule.json", b'{"a": ' + b"[" * 100_000 + b"]" * 100_000 + b"}"),
    ],
    ids=[
        "yaml-syntax",
        "yaml-list",
        "yaml-empty",
        "yaml-not-utf8",
        "yaml-nested-too-deep",
        "json-list",
        "json-truncated",
        "json-huge-integer",
        "json-nested-too-deep",
    ],
)
def test_a_malformed_manifest_is_refused_as_unreadable(
    runs: Path, filename: str, content: bytes
) -> None:
    """Refused with the stable code, not an ``internal_error`` from a parser crash."""
    capsule = runs / "bad"
    capsule.mkdir()
    (capsule / filename).write_bytes(content)
    responses, _ = _session(
        [runs],
        [
            _tool_call(1, "get_capsule_summary", {"capsule": "bad"}),
            _request(2, "ping"),
        ],
    )
    is_error, payload = _tool_payload(responses[0])
    assert (is_error, payload["error"]["code"]) == (True, "capsule_unreadable")
    assert responses[1]["result"] == {}


def test_an_over_size_manifest_is_refused(runs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_capsule_manifest, "_MAX_MANIFEST_BYTES", 1024)
    write_capsule(runs, "big", session_id="s" * 4096)
    responses, _ = _session([runs], [_tool_call(1, "get_capsule_summary", {"capsule": "big"})])
    is_error, payload = _tool_payload(responses[0])
    assert (is_error, payload["error"]["code"]) == (True, "capsule_unreadable")


def test_a_yaml_alias_bomb_is_bounded_not_expanded(runs: Path) -> None:
    """A few KiB of anchors once expanded to n**3 values (and seconds to minutes of work)."""
    n = 60
    leaf = ", ".join(f"k{i}: 1" for i in range(n))
    mid = ", ".join(f"k{i}: *a" for i in range(n))
    top = ", ".join(f"k{i}: *b" for i in range(n))
    capsule = runs / "bomb"
    capsule.mkdir()
    (capsule / "capsule.yaml").write_text(
        f"x: &a {{{leaf}}}\ny: &b {{{mid}}}\nusage_totals: {{{top}}}\n"
        "alias: *b\nrun_id: bomb\nstatus: success\n"
    )
    assert (capsule / "capsule.yaml").stat().st_size < 8 * 1024

    responses, text = _session([runs], [_tool_call(1, "get_capsule_summary", {"capsule": "bomb"})])
    is_error, summary = _tool_payload(responses[0])
    assert is_error is False  # a bounded summary, not a response_too_large refusal
    assert summary["fields"]["usage_totals"] == evidence_tools.TOO_LARGE
    assert summary["fields"]["alias"] == evidence_tools.TOO_LARGE
    assert summary["fields"]["run_id"] == "bomb"
    assert len(text) < 16 * 1024


def test_a_value_within_the_budget_is_returned_whole(runs: Path) -> None:
    usage = {f"model-{i}": {"input_tokens": i, "output_tokens": 2 * i} for i in range(100)}
    write_capsule(runs, "ok", usage_totals=usage)
    summary = EvidenceScope.from_paths([runs]).get_capsule_summary("ok")
    assert summary["fields"]["usage_totals"] == usage


@pytest.mark.parametrize("offset", range(evidence_tools.MAX_STRING_CHARS - 64, 520, 5))
def test_a_secret_across_the_string_cap_is_never_partly_returned(runs: Path, offset: int) -> None:
    padding = ("pad " * 200)[:offset]
    write_capsule(runs, "edge", alias=padding + SECRET)
    alias = EvidenceScope.from_paths([runs]).get_capsule_summary("edge")["fields"]["alias"]
    assert len(alias) <= evidence_tools.MAX_STRING_CHARS
    assert "sk-proj" not in alias
    assert "A1b2" not in alias


def test_a_scope_path_across_the_string_cap_is_replaced_not_cut(runs: Path) -> None:
    root = os.path.realpath(runs)
    padding = "p" * 490
    write_capsule(runs, "edge", session_id=f"{padding}{root}/edge")
    session_id = EvidenceScope.from_paths([runs]).get_capsule_summary("edge")["fields"][
        "session_id"
    ]
    assert session_id == f"{padding}<scope 0>/edge"


# -- adversarial: seal failure modes -----------------------------------------


def _bad_signature(capsule: Path) -> None:
    path = capsule / ".seal" / "manifest.dsse"
    envelope = json.loads(path.read_bytes())
    envelope["signatures"][0]["sig"] = base64.urlsafe_b64encode(b"\x00" * 64).decode()
    path.write_text(json.dumps(envelope))


def _truncated_envelope(capsule: Path) -> None:
    path = capsule / ".seal" / "manifest.dsse"
    path.write_bytes(path.read_bytes()[:40])


def _empty_seal(capsule: Path) -> None:
    shutil.rmtree(capsule / ".seal")
    (capsule / ".seal").mkdir()


def _missing_evidence(capsule: Path) -> None:
    (capsule / "model-calls.jsonl").unlink()


def _edited_evidence(capsule: Path) -> None:
    with (capsule / "model-calls.jsonl").open("a") as fh:
        fh.write("{}\n")


def _no_seal(capsule: Path) -> None:
    shutil.rmtree(capsule / ".seal")


@pytest.mark.parametrize(
    ("damage", "sealed", "check"),
    [
        (_no_seal, False, None),
        (_empty_seal, True, "signature_ok"),
        (_bad_signature, True, "signature_ok"),
        (_truncated_envelope, True, "signature_ok"),
        (_missing_evidence, True, "capsule_binding_ok"),  # partial provenance
        (_edited_evidence, True, "capsule_binding_ok"),
    ],
    ids=["no-seal", "empty-seal", "bad-signature", "truncated-envelope", "missing", "edited"],
)
def test_every_seal_failure_is_reported_invalid_with_reasons(
    runs: Path,
    make_sealed: Callable[[Path, str], Path],
    damage: Callable[[Path], None],
    sealed: bool,
    check: str | None,
) -> None:
    damage(make_sealed(runs, "c1"))
    responses, _ = _session([runs], [_tool_call(1, "verify_capsule", {"capsule": "c1"})])
    is_error, report = _tool_payload(responses[0])
    assert is_error is False  # a verification result, not a refusal
    assert report["valid"] is False
    assert report["sealed"] is sealed
    assert report["errors"]
    assert report["verification_level"] == evidence_tools.VERIFICATION_LEVEL
    if check is not None:
        assert report[check] is False


def test_a_large_trace_is_verified_without_entering_the_result(runs: Path, seal_work: Path) -> None:
    capsule = write_capsule(runs, "large")
    line = json.dumps({"messages": [{"role": "user", "content": PROMPT * 40}]}) + "\n"
    with (capsule / "model-calls.jsonl").open("a") as fh:
        for _ in range(4 * 1024 * 1024 // len(line)):
            fh.write(line)
    seal(capsule, seal_work)
    responses, text = _session(
        [runs],
        [
            _tool_call(1, "verify_capsule", {"capsule": "large"}),
            _tool_call(2, "get_capsule_summary", {"capsule": "large"}),
        ],
    )
    is_error, report = _tool_payload(responses[0])
    assert (is_error, report["valid"]) == (False, True)
    assert len(text) < 32 * 1024
    assert PROMPT not in text


# -- offline -----------------------------------------------------------------


def test_a_whole_session_runs_offline_and_starts_no_process(
    runs: Path, make_sealed: Callable[[Path, str], Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    make_sealed(runs, "c1")
    write_capsule(runs, "c2")
    attempts: list[str] = []

    def refuse(what: str) -> Callable[..., Any]:
        def _refuse(*_: object, **__: object) -> Any:
            attempts.append(what)
            raise OSError(f"conformance gate: {what} is not allowed")

        return _refuse

    monkeypatch.setattr(socket.socket, "connect", refuse("socket.connect"))
    monkeypatch.setattr(socket.socket, "connect_ex", refuse("socket.connect_ex"))
    monkeypatch.setattr(socket, "create_connection", refuse("socket.create_connection"))
    monkeypatch.setattr(socket, "getaddrinfo", refuse("socket.getaddrinfo"))
    monkeypatch.setattr(subprocess, "Popen", refuse("subprocess.Popen"))
    monkeypatch.setattr(os, "system", refuse("os.system"))

    responses, _ = _session(
        [runs],
        [
            _request(1, "tools/list"),
            _tool_call(2, "list_capsules"),
            _tool_call(3, "get_capsule_summary", {"capsule": "c1"}),
            _tool_call(4, "verify_capsule", {"capsule": "c1"}),
            _tool_call(5, "verify_capsule", {"capsule": "c2"}),
        ],
    )
    assert attempts == []
    for response in responses[1:]:
        assert _tool_payload(response)[0] is False
    assert _tool_payload(responses[3])[1]["valid"] is True
