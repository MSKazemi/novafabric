from __future__ import annotations

import asyncio
import json
import logging
import sys
import types
from pathlib import Path
from unittest.mock import patch

import jsonschema
import pytest

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


# ── fail-open: a recording failure never reaches the workload ───────────────
#
# Invariant (ADR-0021): capture never blocks the user workload on component
# failure. The hooked ``call_tool`` must return — or raise — exactly what the
# un-hooked call would have, whatever the capsule writer does.


class _BrokenWriter:
    """A capsule writer whose every append fails like a full disk."""

    def __init__(self) -> None:
        self.appends = 0

    def append_tool_call(self, record: dict) -> None:  # type: ignore[type-arg]
        self.appends += 1
        raise OSError(28, "No space left on device")


class _FailFirstWriter:
    """Fails the first append, then delegates to a real writer."""

    def __init__(self, real: CapsuleWriter) -> None:
        self._real = real
        self._failed = False

    def append_tool_call(self, record: dict) -> None:  # type: ignore[type-arg]
        if not self._failed:
            self._failed = True
            raise OSError(5, "Input/output error")
        self._real.append_tool_call(record)


class _ToolBoom(Exception):
    """The user's tool failed — capture must hand back this exact object."""


class _ScriptedSession:
    """Returns ``outcome`` (or raises it, if it is an exception) from ``call_tool``."""

    server_name = "scripted"
    server_version = "0.0.1"
    outcome: object = None

    async def call_tool(self, name: str, arguments: dict | None = None) -> object:  # type: ignore[type-arg]
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


def _scripted_modules() -> dict[str, object]:
    mods = _patched_mcp_modules()
    mods["mcp.client.session"].ClientSession = _ScriptedSession  # type: ignore[attr-defined]
    return mods


def _call_hooked(writer: object, outcome: object, *, calls: int = 1) -> tuple[object, object]:
    """Run ``calls`` hooked ``call_tool``s; return (hook, last result)."""
    from novafabric.capture.hooks._mcp import MCPHook

    hook = MCPHook(writer=writer, parent_span_id="0" * 16)  # type: ignore[arg-type]
    _ScriptedSession.outcome = outcome
    result: object = None
    with patch.dict(sys.modules, _scripted_modules()):
        hook.install()
        try:
            for _ in range(calls):
                result = asyncio.run(_ScriptedSession().call_tool("search", {"q": "x"}))
        finally:
            hook.uninstall()
            _ScriptedSession.outcome = None
    return hook, result


def test_writer_failure_never_replaces_a_successful_result() -> None:
    response = _FakeResult([_FakeContent("42")])
    writer = _BrokenWriter()

    _, result = _call_hooked(writer, response)

    assert result is response
    assert writer.appends == 1  # one attempt; no second, mislabelled "error" record


def test_writer_failure_never_replaces_the_tools_own_exception() -> None:
    boom = _ToolBoom("upstream 503")

    with pytest.raises(_ToolBoom) as excinfo:
        _call_hooked(_BrokenWriter(), boom)

    assert excinfo.value is boom


def test_failed_success_record_is_not_rewritten_as_a_tool_error(tmp_path: Path) -> None:
    """The tool succeeded: the capsule must not claim it failed with OSError."""
    real = _make_writer(tmp_path)
    response = _FakeResult([_FakeContent("ok")])

    _, result = _call_hooked(_FailFirstWriter(real), response)

    assert result is response
    path = tmp_path / RUN_ID / "tool-calls.jsonl"
    records = _tool_calls(tmp_path) if path.exists() else []
    assert [r for r in records if r["status"] == "error"] == []


def test_unserializable_response_is_returned_unchanged(tmp_path: Path) -> None:
    class _HostileResult:
        @property
        def content(self) -> list[object]:
            raise RuntimeError("lazy content blew up")

    response = _HostileResult()

    _, result = _call_hooked(_make_writer(tmp_path), response)

    assert result is response


def test_dropped_record_is_counted_on_the_capture_recorder(tmp_path: Path) -> None:
    """The count joins the recorder's drops and its capture-health report."""
    from novafabric.capture.event_recorder import (
        EventRecorder,
        bind_capture,
        unbind_capture,
    )

    recorder = EventRecorder(tmp_path, run_id=RUN_ID, capsule_id="cap-1")
    handle = bind_capture(recorder=recorder)
    try:
        hook, _ = _call_hooked(_BrokenWriter(), _FakeResult([]), calls=2)
    finally:
        unbind_capture(handle)

    assert hook.dropped_records == 2  # type: ignore[attr-defined]
    assert recorder.drop_counts == {"tool-calls.jsonl": 2}
    recorder.finalize_health()
    health = json.loads((tmp_path / "capture-health.json").read_text())
    assert health["dropped_events"] == {"tool-calls.jsonl": 2}


def test_dropped_record_logs_one_structured_warning(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="novafabric.capture.hooks._mcp"):
        hook, _ = _call_hooked(_BrokenWriter(), _FakeResult([]), calls=3)

    warnings = [r for r in caplog.records if r.name == "novafabric.capture.hooks._mcp"]
    assert len(warnings) == 1  # bounded: a dead disk must not flood the log
    [rec] = warnings
    assert rec.levelno == logging.WARNING
    assert rec.nova_event == "capture.record_dropped"  # type: ignore[attr-defined]
    assert rec.hook == "mcp"  # type: ignore[attr-defined]
    assert rec.stream == "tool-calls.jsonl"  # type: ignore[attr-defined]
    assert rec.error_type == "OSError"  # type: ignore[attr-defined]
    assert "No space left" not in rec.getMessage()  # type only, never the message
    assert hook.dropped_records == 3  # type: ignore[attr-defined]


def test_cancellation_still_propagates_and_records_nothing() -> None:
    writer = _BrokenWriter()

    with pytest.raises(asyncio.CancelledError):
        _call_hooked(writer, asyncio.CancelledError())

    assert writer.appends == 0
