"""A LangChain tool whose body made model and tool calls (a sub-agent tool).

The shape is deepagents' ``task``: a LangGraph agent delegates to a plain
``@tool`` whose body runs a *sub-agent* -- a LangChain chat model plus an inner
``@tool`` -- and returns the sub-agent's answer. Before ADR-0306 r3b, capture
recorded the outer call with ``nested_records`` and marked it not servable
("nested-call coverage is not implemented"), so a strict replay refused it.

Now the outer call is served like a ``record.tool`` boundary (ADR-0306 slice 4):
its body never runs, so the sub-agent's model calls and tool calls are never
requested; they are consumed as *covered* (``model_covered`` /
``tool_covered``), never counted unconsumed, and the verdict stays faithful.
A nested call that capture could not mark stays unconsumed: the replay fails
closed.

The chat model is a real ``langchain_core`` ``BaseChatModel`` subclass that
calls the real ``openai`` SDK over an ``httpx.MockTransport`` (the shape of
``langchain_openai.ChatOpenAI``, which is not a dependency), so every model
call is recorded and served. No network, no API key.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import jsonschema
import pytest
import yaml
from typer.testing import CliRunner

from novafabric.cli.main import app

pytest.importorskip("langchain_core")
pytest.importorskip("langgraph")
pytest.importorskip("openai")
pytest.importorskip("httpx")

runner = CliRunner()

REPO = Path(__file__).resolve().parents[2]
_RESULT_SCHEMA = json.loads(
    (REPO / "src" / "novafabric" / "schemas" / "replay-result.schema.json").read_text()
)
_TOOL_SCHEMA = json.loads(
    (REPO / "src" / "novafabric" / "schemas" / "tool-call.schema.json").read_text()
)

LC = "langchain_core.tools.BaseTool"
WITHIN = "io.novafabric.within_tool_call_id"
NESTED = "io.novafabric.nested_records"

AGENT = '''
import asyncio, json, os
from typing import Any

import httpx
from openai import OpenAI
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from langgraph.graph import START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition

SIDE = os.environ["AGENT_SIDE"]
OUT = os.environ["AGENT_OUT"]
MODE = os.environ.get("AGENT_MODE", "invoke")
CANNED = json.loads(os.environ.get("AGENT_CANNED") or "null")
SERVED = [0]


def side(text):
    with open(SIDE, "a") as fh:
        fh.write(text + "\\n")


def transport(request):
    if CANNED is None:
        raise RuntimeError("network reached during replay")
    body = CANNED[SERVED[0]]
    SERVED[0] += 1
    return httpx.Response(200, json=body)


CLIENT = OpenAI(api_key="sk-test", max_retries=0,
                http_client=httpx.Client(transport=httpx.MockTransport(transport)))


def to_openai(m):
    if isinstance(m, HumanMessage):
        return {"role": "user", "content": m.content}
    if isinstance(m, ToolMessage):
        return {"role": "tool", "tool_call_id": m.tool_call_id, "content": m.content}
    return {"role": "assistant", "content": m.content or None, "tool_calls": [
        {"id": c["id"], "type": "function",
         "function": {"name": c["name"], "arguments": json.dumps(c["args"])}}
        for c in m.tool_calls] or None}


class ScriptedChat(BaseChatModel):
    """A LangChain chat model over the openai SDK (the ChatOpenAI shape)."""

    tool_specs: list = []

    @property
    def _llm_type(self) -> str:
        return "scripted-openai"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs: Any):
        r = CLIENT.chat.completions.create(
            model="gpt-4o", tools=self.tool_specs or None,
            messages=[to_openai(m) for m in messages])
        msg = r.choices[0].message
        calls = [{"name": c.function.name, "args": json.loads(c.function.arguments),
                  "id": c.id} for c in (msg.tool_calls or [])]
        return ChatResult(generations=[ChatGeneration(
            message=AIMessage(msg.content or "", tool_calls=calls))])


def spec(t):
    return {"type": "function", "function": {
        "name": t.name, "description": t.description,
        "parameters": t.tool_call_schema.model_json_schema()}}


@tool
def lookup(order_id: str) -> dict:
    """Look up an order (called by the sub-agent only)."""
    side(f"lookup {order_id}")
    return {"status": "shipped", "id": order_id}


SUB_MODEL = ScriptedChat(tool_specs=[spec(lookup)])


@tool
def task(description: str) -> str:
    """Delegate a task to a sub-agent (the deepagents `task` shape)."""
    side(f"task {description}")
    messages = [HumanMessage(description)]
    for _ in range(4):
        ai = SUB_MODEL.invoke(messages)
        messages.append(ai)
        if not ai.tool_calls:
            return "subagent: " + ai.content
        for call in ai.tool_calls:
            messages.append(lookup.invoke({**call, "type": "tool_call"}))
    return "subagent: gave up"


OUTER_MODEL = ScriptedChat(tool_specs=[spec(task)])


def agent(state):
    return {"messages": [OUTER_MODEL.invoke(state["messages"])]}


graph = StateGraph(MessagesState)
graph.add_node("agent", agent)
graph.add_node("tools", ToolNode([task]))
graph.add_edge(START, "agent")
graph.add_conditional_edges("agent", tools_condition)
graph.add_edge("tools", "agent")
APP = graph.compile()


def main():
    out = []
    try:
        state = {"messages": [HumanMessage("where is order 7?")]}
        result = asyncio.run(APP.ainvoke(state)) if MODE == "ainvoke" else APP.invoke(state)
        for m in result["messages"]:
            if isinstance(m, ToolMessage):
                out.append({"name": m.name, "response": m.content, "status": m.status})
        out.append({"final": result["messages"][-1].content})
    except BaseException as exc:  # the workload swallows every failure
        out.append({"error": type(exc).__name__, "message": str(exc)[:300]})
    with open(OUT, "w") as fh:
        json.dump(out, fh)


main()
'''


def _canned() -> list[dict[str, Any]]:
    from _mocked_replay_agent import openai_body

    return [
        # outer model: delegate
        openai_body(tool_calls=[("call_task", "task", {"description": "find order 7"})],
                    rid="outer-1"),
        # sub-agent model: look it up, then answer
        openai_body(tool_calls=[("call_lookup", "lookup", {"order_id": "7"})], rid="sub-1"),
        openai_body(content="order 7 has shipped", rid="sub-2"),
        # outer model: final answer, after the task result came back
        openai_body(content="Order 7 has shipped.", rid="outer-2"),
    ]


class Env:
    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.agent = root / "agent.py"
        self.agent.write_text(AGENT)
        self.side = root / "side.log"
        self.out = root / "out.json"
        self.runs = root / "runs"
        self.replays = root / "replays"
        self._mp = monkeypatch
        monkeypatch.setenv("AGENT_SIDE", str(self.side))
        monkeypatch.setenv("AGENT_OUT", str(self.out))
        monkeypatch.setenv("LANGSMITH_TRACING", "false")
        for var in (
            "FORCE_COLOR", "COLORTERM", "AGENT_MODE", "AGENT_CANNED", "NOVA_CAPTURE_LEVEL",
            "LANGCHAIN_TRACING_V2", "LANGSMITH_API_KEY", "LANGCHAIN_API_KEY",
        ):
            monkeypatch.delenv(var, raising=False)

    def capture(self, level: str | None = "forensic") -> tuple[Path, list[dict[str, Any]]]:
        if level is not None:
            self._mp.setenv("NOVA_CAPTURE_LEVEL", level)
        self._mp.setenv("AGENT_CANNED", json.dumps(_canned()))
        result = runner.invoke(
            app, ["capture", "--output-dir", str(self.runs), sys.executable, str(self.agent)]
        )
        assert result.exit_code == 0, result.output
        (capsule,) = [p for p in self.runs.iterdir() if (p / "capsule.yaml").exists()]
        captured = json.loads(self.out.read_text())
        self.out.unlink()
        if self.side.exists():
            self.side.unlink()
        self._mp.delenv("AGENT_CANNED", raising=False)
        self._mp.delenv("NOVA_CAPTURE_LEVEL", raising=False)
        return capsule, captured

    @staticmethod
    def read(path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    @staticmethod
    def write(path: Path, records: list[dict[str, Any]]) -> None:
        path.write_text("".join(json.dumps(r) + "\n" for r in records))

    def replay(self, capsule: Path, *flags: str) -> tuple[dict[str, Any], list[Any], Any]:
        before = set(self.replays.iterdir()) if self.replays.exists() else set()
        cli = runner.invoke(app, ["replay", str(capsule), "-o", str(self.replays), *flags])
        (replay_dir,) = set(self.replays.iterdir()) - before
        result = yaml.safe_load((replay_dir / "replay_result.yaml").read_text())
        jsonschema.validate(result, _RESULT_SCHEMA)
        observed = json.loads(self.out.read_text()) if self.out.exists() else []
        if self.out.exists():
            self.out.unlink()
        return result, observed, cli

    @property
    def side_effects(self) -> list[str]:
        return self.side.read_text().splitlines() if self.side.exists() else []


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Env:
    return Env(tmp_path, monkeypatch)


def _kinds(result: dict[str, Any]) -> list[str]:
    return [d["kind"] for d in result["replay_contract"]["divergences"]]


def _by_name(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {r["tool_name"]: r for r in records}


# ── capture ──────────────────────────────────────────────────────────────────


def test_capture_marks_the_sub_agent_calls_inside_the_outer_tool(env: Env) -> None:
    capsule, captured = env.capture()
    assert captured[-1] == {"final": "Order 7 has shipped."}
    tools = _by_name(env.read(capsule / "tool-calls.jsonl"))
    assert set(tools) == {"task", "lookup"}
    for rec in tools.values():
        jsonschema.validate(rec, _TOOL_SCHEMA)
    outer, inner = tools["task"], tools["lookup"]
    assert outer["extensions"]["io.novafabric.langchain_tool_seam"] == "observed"
    # Its body made two sub-agent model calls (each an SDK record plus the
    # ADR-0305 transport record beneath it) and one sub-agent tool call.
    assert outer["extensions"][NESTED] == 5
    assert inner["extensions"][WITHIN] == outer["tool_call_id"]
    models = [
        r for r in env.read(capsule / "model-calls.jsonl")
        if (r.get("extensions") or {}).get("io.novafabric.record_role") == "logical"
    ]
    inside = [m for m in models if (m.get("extensions") or {}).get(WITHIN)]
    assert len(models) == 4 and len(inside) == 2
    assert {m["extensions"][WITHIN] for m in inside} == {outer["tool_call_id"]}
    # At forensic the outer result is kept, and nesting no longer blocks serving.
    assert outer["extensions"]["io.novafabric.result_codec"] == "json-v1"
    assert "io.novafabric.not_servable_reason" not in outer["extensions"]
    assert outer["result"] == {"value": "subagent: order 7 has shipped"}


# ── strict replay: the outer call is served, its inner calls are covered ──────


def _assert_served_and_covered(
    result: dict[str, Any], observed: list[Any], captured: list[Any], env: Env, cli: Any,
) -> None:
    assert cli.exit_code == 0, cli.output
    assert result["status"] == "success", result.get("error")
    assert env.side_effects == [], "the outer tool body or the sub-agent's tool ran"
    assert observed == captured  # same tool message and final answer, from the oracle
    contract = result["replay_contract"]
    assert contract["divergences"] == []
    assert result["tool_calls_mocked"] == 1  # only the outer call was asked for
    assert result["tool_calls_live"] == 0
    assert contract["tool_calls_covered"] == 1  # the sub-agent's lookup
    assert contract["tool_calls_by_surface"][LC]["covered"] == 1
    assert contract["tool_calls_unconsumed"] == 0
    assert result["model_calls_mocked"] == 2  # the outer model's two calls
    assert contract["model_calls_covered"] == 2  # the sub-agent's two calls
    assert contract["model_calls_unconsumed"] == 0
    assert result["queues_fully_consumed"] is True
    assert result["fidelity"]["verdict"] == "faithful", result["fidelity"]


@pytest.mark.parametrize("mode", ["invoke", "ainvoke"])
def test_strict_replay_serves_the_outer_tool_and_covers_the_sub_agent_calls(
    env: Env, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    monkeypatch.setenv("AGENT_MODE", mode)
    capsule, captured = env.capture()
    result, observed, cli = env.replay(capsule)  # strict, AGENT_CANNED unset
    _assert_served_and_covered(result, observed, captured, env, cli)

    dry, _, cli = env.replay(capsule, "--dry-run")
    assert cli.exit_code == 0, cli.output
    report = next(env.replays.glob(f"{dry['replay_id']}/dry_run_report.txt")).read_text()
    assert not report.startswith("REFUSED:")
    assert "[MOCK] task" in report


def test_a_default_level_capsule_serves_the_outer_tool_from_the_echo(env: Env) -> None:
    """Below forensic only digests are kept; the outer result came back to the
    outer model in its next request, so it is recovered under its digest."""
    capsule, captured = env.capture(level=None)
    tools = _by_name(env.read(capsule / "tool-calls.jsonl"))
    assert tools["task"]["result"] is None and tools["task"]["extensions"][NESTED] == 5
    result, observed, cli = env.replay(capsule)
    _assert_served_and_covered(result, observed, captured, env, cli)
    assert result["replay_contract"]["tool_calls_mocked_by_source"] == {
        "model-request-echo": 1
    }


def test_a_record_captured_before_nested_serving_is_served_without_a_recapture(
    env: Env,
) -> None:
    """Earlier capture marked the outer record not servable with a fixed nesting
    text and kept its value; that text was policy, not data, so replay strips
    it (the slice-4 ``_only_nested`` rule) and serves the kept value."""
    from novafabric.capture import _tool_codec

    capsule, captured = env.capture()
    path = capsule / "tool-calls.jsonl"
    records = env.read(path)
    for rec in records:
        if rec["tool_name"] == "task":
            ext = rec["extensions"]
            ext["io.novafabric.result_codec"] = "not-servable"
            ext["io.novafabric.not_servable_reason"] = (
                _tool_codec.seam_nested_reason("LangChain tool", ext[NESTED])
            )
    env.write(path, records)
    result, observed, cli = env.replay(capsule)
    _assert_served_and_covered(result, observed, captured, env, cli)


def test_a_legacy_record_with_another_reason_stays_unservable(env: Env) -> None:
    from novafabric.capture import _tool_codec

    capsule, _ = env.capture()
    path = capsule / "tool-calls.jsonl"
    records = env.read(path)
    for rec in records:
        if rec["tool_name"] == "task":
            ext = rec["extensions"]
            ext["io.novafabric.result_codec"] = "not-servable"
            ext["io.novafabric.not_servable_reason"] = (
                "the result is a set, not JSON; "
                + _tool_codec.seam_nested_reason("LangChain tool", ext[NESTED])
            )
    env.write(path, records)
    result, _, cli = env.replay(capsule)
    assert cli.exit_code == 1, cli.output
    assert result["error"]["type"] == "ToolSurfaceNotServed"
    reasons = result["error"]["tool_calls_not_served_by_surface"][LC]["reasons"]
    assert reasons == ["the result is a set, not JSON"]
    assert env.side_effects == []


def test_an_unmarked_inner_model_call_fails_closed(env: Env) -> None:
    """A nested call capture could not mark (made from a raw thread) is never
    covered: it stays in the queue and the strict replay fails."""
    capsule, _ = env.capture()
    path = capsule / "model-calls.jsonl"
    records = env.read(path)
    for rec in records:
        (rec.get("extensions") or {}).pop(WITHIN, None)
    env.write(path, records)
    result, _, _ = env.replay(capsule)
    assert env.side_effects == []
    assert result["replay_contract"]["model_calls_covered"] == 0
    assert result["status"] == "failure"
    assert "model_calls_unconsumed" in _kinds(result)
    assert result["fidelity"]["verdict"] != "faithful"
