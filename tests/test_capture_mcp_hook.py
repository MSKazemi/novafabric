from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path
from unittest.mock import patch

import jsonschema

from novafabric.capture.capsule import CapsuleWriter

RUN_ID = "01HXTEST000000000000000000"
SCHEMA = json.loads(
    (Path(__file__).parents[1] / "src/novafabric/schemas/tool-call.schema.json").read_text()
)


def _make_writer(tmp_path: Path) -> CapsuleWriter:
    w = CapsuleWriter(run_id=RUN_ID, base_dir=tmp_path)
    w.open()
    return w


def _tool_calls(tmp_path: Path) -> list[dict]:  # type: ignore[type-arg]
    text = (tmp_path / RUN_ID / "tool-calls.jsonl").read_text().strip()
    return [json.loads(line) for line in text.splitlines() if line]


class _FakeContent:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class _FakeResult:
    def __init__(self, content: list[_FakeContent], is_error: bool = False) -> None:
        self.content = content
        self.isError = is_error


class _FakeSession:
    """Minimal stand-in for mcp.client.session.ClientSession."""

    server_name = "filesystem"
    server_version = "1.2.3"

    async def call_tool(self, name: str, arguments: dict | None = None) -> _FakeResult:
        if name == "broken":
            raise RuntimeError("server unreachable")
        return _FakeResult(
            content=[_FakeContent(f"tool {name} ok with {arguments}")],
            is_error=False,
        )


def _patched_mcp_modules() -> dict[str, object]:
    fake_session_mod = types.ModuleType("mcp.client.session")
    fake_session_mod.ClientSession = _FakeSession  # type: ignore[attr-defined]
    fake_client_mod = types.ModuleType("mcp.client")
    fake_client_mod.session = fake_session_mod  # type: ignore[attr-defined]
    fake_mcp = types.ModuleType("mcp")
    fake_mcp.client = fake_client_mod  # type: ignore[attr-defined]
    return {
        "mcp": fake_mcp,
        "mcp.client": fake_client_mod,
        "mcp.client.session": fake_session_mod,
    }


class _FakeServerInfo:
    name = "metrics_http"
    version = "0.3.1"


class _FakeInitResult:
    serverInfo = _FakeServerInfo()


class _FakeSessionWithInit(_FakeSession):
    """Session that also exposes initialize() returning serverInfo."""

    async def initialize(self) -> _FakeInitResult:  # noqa: D102
        return _FakeInitResult()


def test_initialize_patches_server_name(tmp_path: Path) -> None:
    from novafabric.capture.hooks._mcp import MCPHook

    writer = _make_writer(tmp_path)
    hook = MCPHook(writer=writer, parent_span_id="aabbccddeeff0011")

    # Patch sys.modules to expose _FakeSessionWithInit as ClientSession.
    fake_session_mod = types.ModuleType("mcp.client.session")
    fake_session_mod.ClientSession = _FakeSessionWithInit  # type: ignore[attr-defined]
    fake_client_mod = types.ModuleType("mcp.client")
    fake_client_mod.session = fake_session_mod  # type: ignore[attr-defined]
    fake_mcp = types.ModuleType("mcp")
    fake_mcp.client = fake_client_mod  # type: ignore[attr-defined]
    modules = {
        "mcp": fake_mcp,
        "mcp.client": fake_client_mod,
        "mcp.client.session": fake_session_mod,
    }

    async def driver() -> None:
        session = _FakeSessionWithInit()
        await session.initialize()
        await session.call_tool("ping", {})

    with patch.dict(sys.modules, modules):
        hook.install()
        asyncio.run(driver())
        hook.uninstall()

    records = _tool_calls(tmp_path)
    assert len(records) == 1
    assert records[0]["mcp"]["server_name"] == "metrics_http"
    assert records[0]["mcp"]["server_version"] == "0.3.1"
    assert records[0]["tool_provider"] == "mcp://metrics_http"


def test_records_successful_call(tmp_path: Path) -> None:
    from novafabric.capture.hooks._mcp import MCPHook

    writer = _make_writer(tmp_path)
    hook = MCPHook(writer=writer, parent_span_id="aabbccddeeff0011")

    with patch.dict(sys.modules, _patched_mcp_modules()):
        hook.install()
        session = _FakeSession()
        asyncio.run(session.call_tool("read_file", {"path": "/tmp/x.txt"}))
        hook.uninstall()

    records = _tool_calls(tmp_path)
    assert len(records) == 1
    rec = records[0]
    assert rec["transport"] == "mcp"
    assert rec["tool_name"] == "read_file"
    assert rec["status"] == "success"
    assert rec["mcp"]["server_name"] == "filesystem"
    assert rec["mcp"]["server_version"] == "1.2.3"
    assert rec["mcp"]["method"] == "tools/call"
    assert rec["mcp"]["envelope"]["params"]["arguments"] == {"path": "/tmp/x.txt"}
    assert rec["mcp"]["response_envelope"] is not None


def test_records_error(tmp_path: Path) -> None:
    from novafabric.capture.hooks._mcp import MCPHook

    writer = _make_writer(tmp_path)
    hook = MCPHook(writer=writer, parent_span_id="0" * 16)

    with patch.dict(sys.modules, _patched_mcp_modules()):
        hook.install()
        session = _FakeSession()
        try:
            asyncio.run(session.call_tool("broken"))
        except RuntimeError:
            pass
        hook.uninstall()

    records = _tool_calls(tmp_path)
    assert len(records) == 1
    assert records[0]["status"] == "error"
    assert records[0]["error"]["type"] == "RuntimeError"
    assert "server unreachable" in records[0]["error"]["message"]


def test_install_uninstall_with_fake_mcp_restores_class(tmp_path: Path) -> None:
    from novafabric.capture.hooks._mcp import MCPHook

    writer = _make_writer(tmp_path)
    hook = MCPHook(writer=writer, parent_span_id="0" * 16)
    original_call_tool = _FakeSession.call_tool

    with patch.dict(sys.modules, _patched_mcp_modules()):
        hook.install()
        assert _FakeSession.call_tool is not original_call_tool
        hook.uninstall()
        assert _FakeSession.call_tool is original_call_tool


def test_install_noop_without_mcp(tmp_path: Path) -> None:
    from novafabric.capture.hooks._mcp import MCPHook

    writer = _make_writer(tmp_path)
    hook = MCPHook(writer=writer, parent_span_id="0" * 16)
    with patch.dict(sys.modules, {"mcp": None, "mcp.client": None,
                                  "mcp.client.session": None}):
        hook.install()  # must not raise
    hook.uninstall()  # idempotent


def test_multiple_calls_yield_multiple_records(tmp_path: Path) -> None:
    from novafabric.capture.hooks._mcp import MCPHook

    writer = _make_writer(tmp_path)
    hook = MCPHook(writer=writer, parent_span_id="0" * 16)

    async def driver() -> None:
        s = _FakeSession()
        await s.call_tool("a", {"x": 1})
        await s.call_tool("b", {"x": 2})
        await s.call_tool("c", {})

    with patch.dict(sys.modules, _patched_mcp_modules()):
        hook.install()
        asyncio.run(driver())
        hook.uninstall()

    records = _tool_calls(tmp_path)
    assert [r["tool_name"] for r in records] == ["a", "b", "c"]


def test_record_validates_against_schema(tmp_path: Path) -> None:
    from novafabric.capture.hooks._mcp import MCPHook

    writer = _make_writer(tmp_path)
    hook = MCPHook(writer=writer, parent_span_id="aabbccddeeff0011")

    with patch.dict(sys.modules, _patched_mcp_modules()):
        hook.install()
        session = _FakeSession()
        asyncio.run(session.call_tool("ping", {"n": 3}))
        hook.uninstall()

    records = _tool_calls(tmp_path)
    jsonschema.validate(records[0], SCHEMA, format_checker=jsonschema.FormatChecker())


def test_install_binds_to_the_real_mcp_sdk_and_restores_it(tmp_path: Path) -> None:
    """The hook must patch the *installed* ``mcp`` SDK, not just a fake.

    Every other test here injects a stub into ``sys.modules``, so none of them
    would notice if ``install()`` silently swallowed an ImportError and did
    nothing against the real library — its ``except (ImportError,
    AttributeError): pass`` makes that failure completely quiet.

    That was not hypothetical: ``tests/mcp/`` shadowed the installed ``mcp``
    distribution, so ``import mcp.client.session`` raised ModuleNotFoundError
    for the entire pytest session and this hook took its no-op branch in every
    run. The directory is now ``tests/mcp_conformance/``; this test fails if
    the shadow ever comes back.
    """
    import mcp.client.session as real_session

    from novafabric.capture.hooks._mcp import MCPHook

    target = real_session.ClientSession
    original_call_tool = target.call_tool
    original_initialize = target.initialize

    writer = _make_writer(tmp_path)
    hook = MCPHook(writer=writer, parent_span_id="aabbccddeeff0011")
    try:
        hook.install()
        assert target.call_tool is not original_call_tool, (
            "install() did not patch the real ClientSession.call_tool — the mcp "
            "SDK is unimportable here, so the hook is a silent no-op"
        )
        assert target.initialize is not original_initialize
        # functools.wraps must keep the wrapper introspectable as the original.
        assert target.call_tool.__name__ == "call_tool"
    finally:
        hook.uninstall()

    assert target.call_tool is original_call_tool
    assert target.initialize is original_initialize
