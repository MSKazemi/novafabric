"""CLI-level tests for ``nova mcp-proxy``.

These exercise the Typer wiring (flag resolution, capsule dir validation,
end-to-end record append) using the same fake-upstream pattern as
``test_mcp_proxy.py``. Driven via :class:`typer.testing.CliRunner`, which
hands the proxy a non-tty stdin/stdout — fine because we feed input via the
runner's ``input=`` argument.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from typer.testing import CliRunner

from novafabric.capture.capsule import CapsuleWriter
from novafabric.cli.main import app

runner = CliRunner()

RUN_ID = "01HXTEST000000000000000000"

# Same fake server as test_mcp_proxy.py, kept in-sync intentionally.
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
        continue
    if method == "initialize":
        resp = {"jsonrpc":"2.0","id":rpc_id,
                "result":{"protocolVersion":"2024-11-05",
                          "serverInfo":{"name":"fake-fs","version":"9.9.9"}}}
    elif method == "tools/call":
        params = msg.get("params") or {}
        resp = {"jsonrpc":"2.0","id":rpc_id,
                "result":{"content":[{"type":"text","text":f"ok:{params.get('name')}"}]}}
    else:
        resp = {"jsonrpc":"2.0","id":rpc_id,"error":{"code":-32601,"message":"x"}}
    sys.stdout.write(json.dumps(resp) + "\n")
    sys.stdout.flush()
"""


def _capsule_dir(tmp_path: Path) -> Path:
    w = CapsuleWriter(run_id=RUN_ID, base_dir=tmp_path)
    w.open()
    return tmp_path / RUN_ID


def _client_input(*messages: dict) -> str:  # type: ignore[type-arg]
    return "".join(json.dumps(m) + "\n" for m in messages)


def test_cli_records_tool_call_with_explicit_capsule_dir(tmp_path: Path) -> None:
    cap_dir = _capsule_dir(tmp_path)
    result = runner.invoke(
        app,
        [
            "mcp-proxy",
            "--capsule-dir", str(cap_dir),
            "--",
            sys.executable, "-c", _FAKE_SERVER_SCRIPT,
        ],
        input=_client_input(
            {"jsonrpc": "2.0", "id": "1", "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": "2", "method": "tools/call",
             "params": {"name": "ping", "arguments": {"n": 1}}},
        ),
    )
    assert result.exit_code == 0, result.output
    records = [
        json.loads(line)
        for line in (cap_dir / "tool-calls.jsonl").read_text().splitlines()
        if line
    ]
    assert len(records) == 1
    assert records[0]["tool_name"] == "ping"
    assert records[0]["extensions"]["io.novafabric.capture_method"] == "proxy"


def test_cli_falls_back_to_env_capsule_dir(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    cap_dir = _capsule_dir(tmp_path)
    monkeypatch.setenv("NOVAFABRIC_CAPSULE_DIR", str(cap_dir))
    result = runner.invoke(
        app,
        ["mcp-proxy", "--", sys.executable, "-c", _FAKE_SERVER_SCRIPT],
        input=_client_input(
            {"jsonrpc": "2.0", "id": "1", "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": "2", "method": "tools/call",
             "params": {"name": "ping", "arguments": {}}},
        ),
    )
    assert result.exit_code == 0, result.output
    records = [
        json.loads(line)
        for line in (cap_dir / "tool-calls.jsonl").read_text().splitlines()
        if line
    ]
    assert len(records) == 1


def test_cli_missing_capsule_dir_auto_allocates(
    tmp_path: Path, monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    """v0.6.5 ergonomic improvement: when neither --capsule-dir nor
    NOVAFABRIC_CAPSULE_DIR is set, the proxy auto-allocates a fresh
    capsule under $PWD/.novafabric/runs/<ulid>/. The fake stdio
    upstream below exits immediately so the proxy exits cleanly."""
    monkeypatch.delenv("NOVAFABRIC_CAPSULE_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    runner.invoke(
        app,
        # Upstream that exits without writing — proxy exits cleanly.
        ["mcp-proxy", "--", sys.executable, "-c", "pass"],
    )
    # Either the proxy ran cleanly OR the wiring at least succeeded
    # past capsule-resolution. Auto-allocation worked if a runs dir
    # got created.
    runs = tmp_path / ".novafabric" / "runs"
    assert runs.exists() and runs.is_dir(), (
        "auto-allocation did not create $PWD/.novafabric/runs/"
    )


def test_cli_nonexistent_capsule_dir_auto_creates(tmp_path: Path) -> None:
    """v0.6.5: an explicit --capsule-dir pointing at a path that doesn't
    exist gets created (idempotent mkdir). Previously this errored."""
    target = tmp_path / "manual-allocated-capsule"
    assert not target.exists()
    result = runner.invoke(
        app,
        [
            "mcp-proxy",
            "--capsule-dir", str(target),
            "--",
            sys.executable, "-c", "pass",
        ],
    )
    # Whether the proxy itself exited 0 or non-zero (depends on the
    # fake upstream's behavior), the directory must have been created.
    assert target.exists() and target.is_dir(), (
        f"--capsule-dir {target} was not auto-created\n{result.output}"
    )
