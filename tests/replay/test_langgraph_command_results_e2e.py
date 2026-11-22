"""LangChain tools that return a LangGraph ``Command`` (a state update) are
recorded and served (ADR-0306 r3b).

A tool may return ``langgraph.types.Command(update={...}, goto=...)`` instead of a
value: ``ToolNode`` validates it (the update must carry a ``ToolMessage``
answering the call) and LangGraph applies the update to the graph state and
routes to ``goto``. Before, capture recorded such a result as "not a JSON value"
and it was never served, even at ``forensic``.

Capture now records a JSON envelope (``graph``, ``goto``, ``resume`` and the
``update`` entries, each a JSON value or a list of ``ToolMessage`` envelopes
answering this call), and replay rebuilds a real ``Command`` whose messages
answer the replayed call's ``tool_call_id``. A ``Send`` in ``goto``, an update
value that is neither JSON nor a list of such messages, and a message answering
another call keep the record not servable, with a reason naming the field.

The workload is a real ``StateGraph`` with a ``ToolNode`` and a scripted model;
the oracle is the final graph state, written by the workload itself.
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

runner = CliRunner()

REPO = Path(__file__).resolve().parents[2]
_RESULT_SCHEMA = json.loads(
    (REPO / "src" / "novafabric" / "schemas" / "replay-result.schema.json").read_text()
)
_TOOL_SCHEMA = json.loads(
    (REPO / "src" / "novafabric" / "schemas" / "tool-call.schema.json").read_text()
)

LC = "langchain_core.tools.BaseTool"
KIND = "io.novafabric.langchain_result_kind"

AGENT = '''
import asyncio, json, os
from typing import Annotated, Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.graph import START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition
from langgraph.types import Command, Send

SIDE = os.environ["AGENT_SIDE"]
OUT = os.environ["AGENT_OUT"]
MODE = os.environ.get("AGENT_MODE", "invoke")


def side(text):
    with open(SIDE, "a") as fh:
        fh.write(text + "\\n")


def merge(left, right):
    return {**(left or {}), **(right or {})}


class State(MessagesState):
    notes: Annotated[dict, merge]
    audited: list


@tool
def save_note(key: str, text: str,
              tool_call_id: Annotated[str, InjectedToolCallId]) -> Command:
    """Save a note into the graph state."""
    side(f"save_note {key}")
    return Command(update={
        "notes": {key: {"text": text, "words": len(text.split())}},
        "messages": [ToolMessage(f"saved {key}", tool_call_id=tool_call_id)],
    })


@tool
def escalate(reason: str, tool_call_id: Annotated[str, InjectedToolCallId]) -> Command:
    """Escalate: also route to the audit node."""
    side(f"escalate {reason}")
    return Command(
        update={"messages": [ToolMessage(
            f"escalated: {reason}", tool_call_id=tool_call_id, status="error",
            artifact={"ticket": 7},
        )]},
        goto="audit",
    )


@tool
def fan_out(n: int, tool_call_id: Annotated[str, InjectedToolCallId]) -> Command:
    """Route through Send (not servable)."""
    side(f"fan_out {n}")
    return Command(
        update={"messages": [ToolMessage("fanned", tool_call_id=tool_call_id)]},
        goto=[Send("audit", {"audited": ["sent"]})],
    )


@tool
def opaque(tool_call_id: Annotated[str, InjectedToolCallId]) -> Command:
    """An update value that is not JSON (not servable)."""
    side("opaque")
    return Command(update={
        "notes": {"when": {1, 2}},
        "messages": [ToolMessage("opaque", tool_call_id=tool_call_id)],
    })


def plan_model(state):
    if any(isinstance(m, ToolMessage) for m in state["messages"]):
        return {"messages": [AIMessage("done")]}
    plan = json.loads(os.environ["AGENT_PLAN"])
    return {"messages": [AIMessage("", tool_calls=[
        {"name": op["tool"], "args": op.get("args", {}), "id": f"call-{i}"}
        for i, op in enumerate(plan)
    ])]}


def audit(state):
    return {"audited": [*(state.get("audited") or []), "audit"]}


graph = StateGraph(State)
graph.add_node("agent", plan_model)
graph.add_node("tools", ToolNode([save_note, escalate, fan_out, opaque]))
graph.add_node("audit", audit)
graph.add_edge(START, "agent")
graph.add_conditional_edges("agent", tools_condition)
graph.add_edge("tools", "agent")
APP = graph.compile()


def view(state):
    return {
        "notes": state.get("notes") or {},
        "audited": state.get("audited") or [],
        "messages": [
            {"type": m.type, "content": m.content, "name": getattr(m, "name", None),
             "tool_call_id": getattr(m, "tool_call_id", None),
             "status": getattr(m, "status", None),
             "artifact": getattr(m, "artifact", None)}
            for m in state["messages"]
        ],
    }


def main():
    try:
        start = {"messages": [HumanMessage("go")], "notes": {}, "audited": []}
        result = asyncio.run(APP.ainvoke(start)) if MODE == "ainvoke" else APP.invoke(start)
        out = {"state": view(result)}
    except BaseException as exc:  # the workload swallows every failure
        out = {"error": type(exc).__name__, "message": str(exc)[:300]}
    with open(OUT, "w") as fh:
        json.dump(out, fh, default=repr)


main()
'''


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
        for var in ("FORCE_COLOR", "COLORTERM", "AGENT_MODE", "NOVA_CAPTURE_LEVEL",
                    "LANGCHAIN_TRACING_V2", "LANGSMITH_API_KEY", "LANGCHAIN_API_KEY"):
            monkeypatch.delenv(var, raising=False)

    def capture(
        self, plan: list[dict[str, Any]], level: str | None = "forensic",
    ) -> tuple[Path, dict[str, Any]]:
        self._mp.setenv("AGENT_PLAN", json.dumps(plan))
        if level is not None:
            self._mp.setenv("NOVA_CAPTURE_LEVEL", level)
        result = runner.invoke(
            app, ["capture", "--output-dir", str(self.runs), sys.executable, str(self.agent)]
        )
        assert result.exit_code == 0, result.output
        (capsule,) = [p for p in self.runs.iterdir() if (p / "capsule.yaml").exists()]
        captured = json.loads(self.out.read_text())
        self.out.unlink()
        if self.side.exists():
            self.side.unlink()
        self._mp.delenv("NOVA_CAPTURE_LEVEL", raising=False)
        return capsule, captured

    def records(self, capsule: Path) -> dict[str, dict[str, Any]]:
        lines = (capsule / "tool-calls.jsonl").read_text().splitlines()
        return {r["tool_name"]: r for r in (json.loads(x) for x in lines if x.strip())}

    def write_records(self, capsule: Path, records: list[dict[str, Any]]) -> None:
        (capsule / "tool-calls.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records))

    def replay(self, capsule: Path, *flags: str) -> tuple[dict[str, Any], Any, Any]:
        before = set(self.replays.iterdir()) if self.replays.exists() else set()
        cli = runner.invoke(app, ["replay", str(capsule), "-o", str(self.replays), *flags])
        (replay_dir,) = set(self.replays.iterdir()) - before
        result = yaml.safe_load((replay_dir / "replay_result.yaml").read_text())
        jsonschema.validate(result, _RESULT_SCHEMA)
        observed = json.loads(self.out.read_text()) if self.out.exists() else None
        if self.out.exists():
            self.out.unlink()
        return result, observed, cli

    @property
    def side_effects(self) -> list[str]:
        return self.side.read_text().splitlines() if self.side.exists() else []


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Env:
    return Env(tmp_path, monkeypatch)


def _call(tool: str, **args: Any) -> dict[str, Any]:
    return {"tool": tool, "args": args}


PLAN = [_call("save_note", key="job-1", text="OOM on node 4"), _call("escalate", reason="OOM")]


def test_a_command_result_is_recorded_as_a_json_envelope(env: Env) -> None:
    capsule, captured = env.capture(PLAN)
    state = captured["state"]
    assert state["notes"] == {"job-1": {"text": "OOM on node 4", "words": 4}}
    assert state["audited"] == ["audit"]  # goto="audit" ran the audit node
    records = env.records(capsule)
    for rec in records.values():
        jsonschema.validate(rec, _TOOL_SCHEMA)
    note = records["save_note"]
    ext = note["extensions"]
    assert ext[KIND] == "langgraph_command"
    assert ext["io.novafabric.result_codec"] == "json-v1", ext.get(
        "io.novafabric.not_servable_reason"
    )
    value = note["result"]["value"]
    assert value["graph"] is None and value["goto"] == [] and value["resume"] is None
    # The message answering this call is kept without the call id it answers.
    entries = dict(value["update"]["entries"])
    assert entries["notes"] == {"json": {"job-1": {"text": "OOM on node 4", "words": 4}}}
    (message,) = entries["messages"]["messages"]
    assert message["content"] == "saved job-1"
    assert "tool_call_id" not in message and "call-0" not in json.dumps(value)
    assert records["escalate"]["result"]["value"]["goto"] == "audit"


@pytest.mark.parametrize("mode", ["invoke", "ainvoke"])
def test_strict_replay_serves_the_command_and_reaches_the_same_final_state(
    env: Env, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    monkeypatch.setenv("AGENT_MODE", mode)
    capsule, captured = env.capture(PLAN)
    result, observed, cli = env.replay(capsule)  # strict
    assert cli.exit_code == 0, cli.output
    assert result["status"] == "success", result.get("error")
    assert env.side_effects == [], "a Command tool body ran during mocked replay"
    # The oracle: the final graph state the workload wrote -- notes merged by the
    # reducer, the audit node reached through goto, the ToolMessages (content,
    # name, status, artifact, tool_call_id) -- equals the captured one.
    assert observed == captured
    assert result["tool_calls_mocked"] == 2
    assert result["tool_calls_live"] == 0
    contract = result["replay_contract"]
    assert contract["divergences"] == []
    assert contract["tool_calls_unconsumed"] == 0
    assert result["fidelity"]["verdict"] == "faithful", result["fidelity"]


@pytest.mark.parametrize(("tool", "args", "why"), [
    ("fan_out", {"n": 2}, "Command.goto holds a Send"),
    ("opaque", {}, "Command.update['notes']"),
])
def test_a_command_that_cannot_be_rebuilt_is_not_servable_and_says_why(
    env: Env, tool: str, args: dict[str, Any], why: str,
) -> None:
    capsule, _ = env.capture([_call(tool, **args)])
    rec = env.records(capsule)[tool]
    ext = rec["extensions"]
    assert ext[KIND] == "langgraph_command"
    assert ext["io.novafabric.result_codec"] == "not-servable"
    assert why in ext["io.novafabric.not_servable_reason"]
    assert rec["result"] is None
    result, _, cli = env.replay(capsule)
    assert cli.exit_code == 1, cli.output
    assert result["error"]["type"] == "ToolSurfaceNotServed"
    assert why in result["error"]["message"]
    assert env.side_effects == []


def test_a_message_that_answers_another_call_is_never_served(env: Env) -> None:
    capsule, _ = env.capture(PLAN)
    records = env.records(capsule)
    note = records["save_note"]
    for key, entry in note["result"]["value"]["update"]["entries"]:
        if key == "messages":
            entry["messages"][0]["answers"] = "another-call"
    env.write_records(capsule, list(records.values()))
    result, observed, cli = env.replay(capsule)
    assert cli.exit_code == 1, cli.output
    assert result["status"] == "failure"
    assert env.side_effects == []
    (div,) = [
        d for d in result["replay_contract"]["divergences"]
        if d["kind"] == "tool_result_not_servable"
    ]
    assert "Command" in div["reason"]


def test_a_default_level_command_is_digested_and_refused_with_the_recapture_hint(
    env: Env,
) -> None:
    """Below forensic only the envelope's digest is kept; the echo the model
    saw is the ToolMessage content, not the Command, so nothing can be shown
    equal to the recorded value: refused up front, never guessed."""
    capsule, _ = env.capture(PLAN, level=None)
    note = env.records(capsule)["save_note"]
    assert note["result"] is None
    assert note["extensions"]["io.novafabric.result_digest"]
    result, _, cli = env.replay(capsule)
    assert cli.exit_code == 1, cli.output
    assert result["error"]["type"] == "ToolSurfaceNotServed"
    assert "NOVA_CAPTURE_LEVEL=forensic" in result["error"]["message"]
    assert env.side_effects == []
