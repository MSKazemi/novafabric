"""ADR-0306 slice 3 (Google ADK): the tool seam, in process, on a real ``Runner``.

Capture-side record shape, the steps-aside rules, the replay server's id tier
and the "capture never displaces replay" guard. The subprocess round trip
(``nova capture`` -> ``nova replay``) is ``tests/replay/test_adk_tool_replay_e2e.py``.
Every model here is an offline ``BaseLlm`` fake -- no network.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import jsonschema
import pytest

pytest.importorskip("google.adk")

from google.adk.agents import LlmAgent  # noqa: E402
from google.adk.models.base_llm import BaseLlm  # noqa: E402
from google.adk.models.llm_response import LlmResponse  # noqa: E402
from google.adk.plugins.base_plugin import BasePlugin  # noqa: E402
from google.adk.runners import Runner  # noqa: E402
from google.adk.sessions import InMemorySessionService  # noqa: E402
from google.adk.tools.function_tool import FunctionTool  # noqa: E402
from google.genai import types  # noqa: E402

from novafabric.adapters import _adk_tool_seam as seam  # noqa: E402
from novafabric.adapters.google_adk import make_tool_plugin  # noqa: E402
from novafabric.capture import record  # noqa: E402
from novafabric.capture.hooks import _BUILT_IN_HOOKS  # noqa: E402
from novafabric.replay._adk_tool_server import AdkToolServer  # noqa: E402
from novafabric.replay._contract import (  # noqa: E402
    TOOL_SURFACE_ADK,
    ReplayEventLog,
    is_servable_tool_record,
    tool_surface,
)
from novafabric.replay._errors import ReplayToolUnmatchedError  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
_TOOL_SCHEMA = json.loads(
    (REPO / "src" / "novafabric" / "schemas" / "tool-call.schema.json").read_text()
)


class _ListWriter:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def append_tool_call(self, rec: dict[str, Any]) -> None:
        self.records.append(rec)


@pytest.fixture(autouse=True)
def _no_handler_leaks() -> Iterator[None]:
    assert seam._get_handler() is None
    yield
    assert seam._get_handler() is None, "a test left an ADK tool handler registered"


@pytest.fixture
def sink(monkeypatch: pytest.MonkeyPatch) -> Iterator[_ListWriter]:
    monkeypatch.setenv("NOVA_CAPTURE_LEVEL", "forensic")
    writer = _ListWriter()
    hook = seam.AdkToolHook(writer, "0123456789abcdef")  # type: ignore[arg-type]
    hook.install()
    try:
        yield writer
    finally:
        hook.uninstall()


def _run(
    tools: list[Any], plan: list[dict[str, Any]], plugins: list[Any] | None = None
) -> list[Any]:
    """One real ADK invocation; returns the function responses, or the error."""

    class Fake(BaseLlm):
        model: str = "fake-offline"

        async def generate_content_async(  # type: ignore[override]
            self, llm_request: Any, stream: bool = False
        ) -> Any:
            answered = any(
                p.function_response for c in llm_request.contents for p in (c.parts or [])
            )
            if answered:
                yield LlmResponse(
                    content=types.Content(role="model", parts=[types.Part(text="done")])
                )
                return
            yield LlmResponse(content=types.Content(role="model", parts=[
                types.Part(function_call=types.FunctionCall(
                    name=op["tool"], args=op.get("args", {}), id=op.get("id"),
                ))
                for op in plan
            ]))

    async def main() -> list[Any]:
        svc = InMemorySessionService()
        adk_runner = Runner(
            app_name="seam", agent=LlmAgent(name="agent", model=Fake(), tools=tools),
            session_service=svc,
            plugins=plugins if plugins is not None else [make_tool_plugin()],
        )
        session = await svc.create_session(app_name="seam", user_id="u")
        out: list[Any] = []
        try:
            async for event in adk_runner.run_async(
                user_id="u", session_id=session.id,
                new_message=types.Content(role="user", parts=[types.Part(text="go")]),
            ):
                for part in (event.content.parts if event.content else None) or []:
                    if part.function_response is not None:
                        out.append(part.function_response.response)
        except Exception as exc:  # noqa: BLE001
            out.append(exc)
        return out

    return asyncio.run(main())


def _call(tool: str, call_id: str | None = None, **args: Any) -> dict[str, Any]:
    return {"tool": tool, "args": args, "id": call_id}


# ── the plugin ───────────────────────────────────────────────────────────────


def test_make_tool_plugin_is_an_adk_plugin_and_validates_classes() -> None:
    plugin = make_tool_plugin({"lookup": "read-only"})
    assert isinstance(plugin, BasePlugin)
    assert plugin.name == "novafabric_tools"
    assert plugin.mutation_class_for("lookup") == "read-only"
    assert plugin.mutation_class_for("other") == "unknown"
    with pytest.raises(ValueError, match="mutation class"):
        make_tool_plugin({"lookup": "dangerous"})
    with pytest.raises(ValueError, match="mutation class"):
        make_tool_plugin(default_mutation_class="nope")


def test_outside_capture_and_replay_the_plugin_changes_nothing() -> None:
    ran: list[str] = []

    def lookup(order_id: str) -> dict[str, Any]:
        ran.append(order_id)
        return {"id": order_id}

    assert _run([lookup], [_call("lookup", order_id="o-1")]) == [{"id": "o-1"}]
    assert ran == ["o-1"]


def test_the_capture_hook_is_a_built_in_keyed_on_google_adk() -> None:
    assert ("google.adk", "novafabric.adapters._adk_tool_seam", "AdkToolHook") in _BUILT_IN_HOOKS


# ── capture ──────────────────────────────────────────────────────────────────


def test_each_tool_call_writes_one_schema_valid_servable_record(sink: _ListWriter) -> None:
    def lookup(order_id: str) -> dict[str, Any]:
        return {"id": order_id}

    def nothing() -> None:
        return None

    out = _run([lookup, nothing], [_call("lookup", "call-1", order_id="o-1"), _call("nothing")])
    assert out == [{"id": "o-1"}, {"result": None}]
    first, second = sink.records
    for rec in sink.records:
        jsonschema.validate(rec, _TOOL_SCHEMA)
        assert tool_surface(rec) == TOOL_SURFACE_ADK and is_servable_tool_record(rec)
        assert rec["transport"] == "python" and rec["status"] == "success"
        assert rec["tool_provider"] == "google-adk://google.adk.tools.function_tool.FunctionTool"
    assert first["arguments"] == {"order_id": "o-1"}
    assert first["result"] == {"value": {"id": "o-1"}}
    assert first["extensions"]["io.novafabric.adk_function_call_id"] == "call-1"
    assert first["extensions"]["io.novafabric.tool_qualname"].endswith("lookup")
    assert second["result"] == {"value": None}


@pytest.mark.parametrize(
    ("make_tool", "reason"),
    [
        (lambda: _raising, "a recorded ADK tool failure is not served"),
        (lambda: _tuple_result, "is a tuple"),
        (lambda: FunctionTool(_slow), "long-running"),
    ],
    ids=["error", "non-json-result", "long-running"],
)
def test_unservable_calls_are_recorded_with_the_reason(
    sink: _ListWriter, make_tool: Callable[[], Any], reason: str
) -> None:
    tool = make_tool()
    if isinstance(tool, FunctionTool) and tool.func is _slow:
        tool.is_long_running = True
    name = tool.name if isinstance(tool, FunctionTool) else tool.__name__
    _run([tool], [_call(name, x="a")])
    (rec,) = sink.records
    jsonschema.validate(rec, _TOOL_SCHEMA)
    assert not is_servable_tool_record(rec)
    assert reason in rec["extensions"]["io.novafabric.not_servable_reason"]


def _raising(x: str) -> dict[str, Any]:
    raise ValueError("bad " + x)


def _tuple_result(x: str) -> Any:
    return (x, x)


def _slow(x: str) -> dict[str, Any]:
    return {"pending": x}


def test_the_seam_steps_aside_for_record_tool_functions_and_mcp_tools(
    sink: _ListWriter,
) -> None:
    @record.tool(mutation_class="none")
    def declared(x: str) -> dict[str, Any]:
        return {"x": x}

    assert seam.skip_reason(FunctionTool(declared)) == "record.tool"
    _run([declared], [_call("declared", x="a")])
    assert sink.records == [], "a record.tool function is its own boundary"

    from google.adk.tools.mcp_tool.mcp_tool import McpTool

    mcp_like = McpTool.__new__(McpTool)
    assert seam.skip_reason(mcp_like) == "mcp"


def test_payloads_off_keeps_digests_only(
    sink: _ListWriter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOVA_CAPTURE_LEVEL", "standard")

    def lookup(order_id: str) -> dict[str, Any]:
        return {"id": order_id}

    _run([lookup], [_call("lookup", order_id="o-1")])
    (rec,) = sink.records
    jsonschema.validate(rec, _TOOL_SCHEMA)
    assert rec["arguments"] == {} and rec["result"] is None
    assert "o-1" not in json.dumps(rec)
    assert rec["extensions"]["io.novafabric.arguments_digest"]
    assert "NOVA_CAPTURE_LEVEL=forensic" in rec["extensions"]["io.novafabric.not_servable_reason"]


def test_a_capture_sink_never_displaces_a_replay_server() -> None:
    """An adapter capture started inside a mocked replay must not turn served
    ADK tools back into live ones."""
    server = AdkToolServer([], divergence_policy="fail", events=ReplayEventLog(None),
                           permitted=frozenset({"none"}))
    previous = seam._set_handler(server)
    try:
        inner = seam.AdkToolHook(_ListWriter(), "0123456789abcdef")  # type: ignore[arg-type]
        inner.install()
        assert seam._get_handler() is server
        inner.uninstall()
        assert seam._get_handler() is server
    finally:
        seam._set_handler(previous)


# ── replay server ────────────────────────────────────────────────────────────


def test_the_id_tier_never_serves_an_answer_to_other_arguments(sink: _ListWriter) -> None:
    def lookup(order_id: str) -> dict[str, Any]:
        return {"id": order_id}

    _run([lookup], [_call("lookup", "call-1", order_id="o-1")])
    records = [dict(r) for r in sink.records]
    sink.records.clear()
    server = AdkToolServer(records, divergence_policy="fail", events=ReplayEventLog(None),
                           permitted=frozenset({"none"}))
    previous = seam._set_handler(server)
    try:
        ran: list[str] = []

        def lookup2(order_id: str) -> dict[str, Any]:
            ran.append(order_id)
            return {"id": order_id}

        lookup2.__name__ = "lookup"
        # Same function_call_id, other arguments: refused, never served "o-1".
        (outcome,) = _run([lookup2], [_call("lookup", "call-1", order_id="o-2")])
        assert isinstance(outcome, RuntimeError)
        assert isinstance(outcome.__cause__, ReplayToolUnmatchedError)
        assert ran == []
        # The recorded call itself is still served by its id.
        assert _run([lookup2], [_call("lookup", "call-1", order_id="o-1")]) == [{"id": "o-1"}]
        assert ran == []
    finally:
        seam._set_handler(previous)
