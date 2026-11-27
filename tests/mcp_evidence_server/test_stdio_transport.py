"""The evidence server as a real stdio process (issue #244).

Two clients drive ``python -m novafabric.mcp.evidence_server``:

* raw newline-delimited JSON-RPC over pipes, checking that stdout carries only
  protocol messages and that the process writes nothing — not into the scope,
  not into an isolated ``HOME``;
* the official MCP Python SDK client (``mcp``, MIT), when it is installed (it
  arrives through the ``openai-agents`` extra), as a conformance check of the
  handshake, ``tools/list`` and ``tools/call``.

No network, no LLM, no API key.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from .conftest import PROMPT, SECRET, snapshot, write_capsule

_MODULE = "novafabric.mcp.evidence_server"


def _isolated_env(home: Path) -> dict[str, str]:
    """A child environment with no ambient NovaFabric state and a throwaway HOME."""
    home.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("NOVAFABRIC_")}
    env.update(
        {
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_DATA_HOME": str(home / ".local" / "share"),
            "XDG_CACHE_HOME": str(home / ".cache"),
            "NOVAFABRIC_HOME": str(home / ".novafabric"),
            "NOVAFABRIC_AUDIT_LOG_PATH": str(home / "audit.jsonl"),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    return env


def _exchange(runs: Path, home: Path, messages: list[object]) -> tuple[list[dict[str, Any]], str]:
    data = "".join(json.dumps(m) + "\n" for m in messages).encode()
    proc = subprocess.run(
        [sys.executable, "-m", _MODULE, "--scope", str(runs)],
        input=data,
        capture_output=True,
        env=_isolated_env(home),
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr.decode(errors="replace")
    lines = proc.stdout.decode().splitlines()
    return [json.loads(line) for line in lines], proc.stderr.decode(errors="replace")


@pytest.mark.timeout(180)
def test_a_stdio_session_lists_summarizes_and_verifies_without_writing(
    tmp_path: Path, runs: Path, sealed: Callable[[Path, str], Path]
) -> None:
    write_capsule(runs, "plain")
    sealed(runs, "signed")
    outside = tmp_path / "outside"
    write_capsule(outside, "private")
    (runs / "escape").symlink_to(outside / "private")
    home = tmp_path / "home"
    home.mkdir()
    before = snapshot(runs)

    call = "tools/call"
    responses, _stderr = _exchange(
        runs,
        home,
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0"},
                },
            },
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": call,
                "params": {"name": "list_capsules", "arguments": {}},
            },
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": call,
                "params": {"name": "get_capsule_summary", "arguments": {"capsule": "plain"}},
            },
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": call,
                "params": {"name": "verify_capsule", "arguments": {"capsule": "signed"}},
            },
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": call,
                "params": {"name": "verify_capsule", "arguments": {"capsule": "escape"}},
            },
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": call,
                "params": {"name": "get_capsule_summary", "arguments": {"capsule": "../outside"}},
            },
        ],
    )

    by_id = {r["id"]: r for r in responses}
    assert sorted(by_id) == [1, 2, 3, 4, 5, 6, 7]  # one line per request, nothing else
    assert by_id[1]["result"]["protocolVersion"] == "2025-11-25"
    listed = by_id[3]["result"]["structuredContent"]["capsules"]
    assert [c["capsule"] for c in listed] == ["plain", "signed"]
    summary = by_id[4]["result"]["structuredContent"]
    assert summary["fields"]["run_id"] == "plain"
    assert by_id[5]["result"]["structuredContent"]["valid"] is True
    assert by_id[6]["result"]["structuredContent"]["error"]["code"] == "outside_scope"
    assert by_id[7]["result"]["structuredContent"]["error"]["code"] == "invalid_argument"

    wire = json.dumps(responses)
    for leaked in (PROMPT, SECRET, str(tmp_path)):
        assert leaked not in wire
    assert snapshot(runs) == before
    assert list(home.rglob("*")) == []


def test_the_process_refuses_to_start_without_a_usable_scope(tmp_path: Path) -> None:
    proc = subprocess.run(
        [sys.executable, "-m", _MODULE, "--scope", str(tmp_path / "missing")],
        input=b"",
        capture_output=True,
        env=_isolated_env(tmp_path / "home"),
        timeout=120,
        check=False,
    )
    assert proc.returncode == 2
    assert proc.stdout == b""
    assert b"not a directory" in proc.stderr


@pytest.mark.timeout(180)
def test_the_official_mcp_client_can_use_the_server(
    tmp_path: Path, runs: Path, sealed: Callable[[Path, str], Path]
) -> None:
    pytest.importorskip("mcp.client.stdio")
    from mcp import ClientSession, StdioServerParameters  # noqa: PLC0415
    from mcp.client.stdio import stdio_client  # noqa: PLC0415

    sealed(runs, "signed")
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", _MODULE, "--scope", str(runs)],
        env=_isolated_env(tmp_path / "home"),
    )

    async def session() -> dict[str, Any]:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as client:
                init = await client.initialize()
                tools = await client.list_tools()
                listed = await client.call_tool("list_capsules", {})
                verified = await client.call_tool("verify_capsule", {"capsule": "signed"})
                refused = await client.call_tool("verify_capsule", {"capsule": "../signed"})
                return {
                    "version": init.protocolVersion,
                    "server": init.serverInfo.name,
                    "tools": [t.name for t in tools.tools],
                    "read_only": [
                        bool(t.annotations and t.annotations.readOnlyHint) for t in tools.tools
                    ],
                    "listed": listed.structuredContent,
                    "verified": verified.structuredContent,
                    "refused": (refused.isError, refused.structuredContent),
                }

    out = asyncio.run(asyncio.wait_for(session(), timeout=120))
    from novafabric.mcp.evidence_server import SUPPORTED_PROTOCOL_VERSIONS  # noqa: PLC0415

    assert out["version"] in SUPPORTED_PROTOCOL_VERSIONS
    assert out["server"] == "novafabric-evidence"
    assert out["tools"] == ["list_capsules", "get_capsule_summary", "verify_capsule"]
    assert all(out["read_only"])
    assert out["listed"]["capsules"] == [{"root": 0, "capsule": "signed", "sealed": True}]
    assert out["verified"]["valid"] is True
    is_error, content = out["refused"]
    assert is_error is True
    assert content["error"]["code"] == "invalid_argument"


@pytest.mark.timeout(180)
def test_hostile_lines_do_not_end_the_process(tmp_path: Path, runs: Path) -> None:
    """Each of these once killed the process (review of #346); now each is answered."""
    write_capsule(runs, "good")
    try:
        os.mkdir(os.fsencode(runs) + b"/caf\xe9")
        with open(os.fsencode(runs) + b"/caf\xe9/capsule.yaml", "wb") as fh:
            fh.write(b"run_id: cafe\n")
    except OSError:  # pragma: no cover - a filesystem that only takes UTF-8 names
        pytest.skip("this filesystem refuses non-UTF-8 file names")
    init = {
        "jsonrpc": "2.0",
        "id": 0,
        "method": "initialize",
        "params": {"protocolVersion": "2025-11-25"},
    }
    raw = (
        b"\n".join(
            [
                json.dumps(init).encode(),
                b'{"jsonrpc":"2.0","id":1,"method":"tools/\\udcff"}',
                b"[" * 200_000 + b"]" * 200_000,
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {"name": "list_capsules", "arguments": {}},
                    }
                ).encode(),
                json.dumps({"jsonrpc": "2.0", "id": 4, "method": "ping"}).encode(),
            ]
        )
        + b"\n"
    )
    proc = subprocess.run(
        [sys.executable, "-m", _MODULE, "--scope", str(runs)],
        input=raw,
        capture_output=True,
        env=_isolated_env(tmp_path / "home"),
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr.decode(errors="replace")
    responses = [json.loads(line) for line in proc.stdout.decode("ascii").splitlines()]
    assert [r.get("id") for r in responses] == [0, 1, None, 3, 4]
    assert responses[1]["error"]["code"] == -32601
    assert responses[2]["error"]["code"] == -32700
    listed = responses[3]["result"]["structuredContent"]
    assert [c["capsule"] for c in listed["capsules"]] == ["good"]
    assert listed["skipped_unaddressable"] == 1
    assert responses[4]["result"] == {}
