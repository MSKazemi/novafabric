"""LangChain tools that return a ``ToolMessage`` themselves are recorded and served
(ADR-0306 r3 amendment, codec "B").

The shape is the one c08's ``ml_reviewer`` workload ran (``deepagents`` 0.7.40
file tools: ``ls`` / ``read_file`` build a ``ToolMessage`` from the injected
tool-call id): before this, every such record said "the result is a
ToolMessage, not a JSON value" and could never be served, even at
``forensic``. ``deepagents`` itself is not a test dependency; the tools here
reproduce its result shape with the real ``langchain-core`` and ``langgraph``.
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

AGENT = '''
import json, os
from pathlib import Path
from typing import Annotated

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.graph import START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition

SIDE = os.environ["AGENT_SIDE"]
OUT = os.environ["AGENT_OUT"]
WORK = Path(os.environ["AGENT_WORK"])


def side(text):
    with open(SIDE, "a") as fh:
        fh.write(text + "\\n")


@tool
def ls(path: str, tool_call_id: Annotated[str, InjectedToolCallId]) -> ToolMessage:
    """List a directory."""
    side(f"ls {path}")
    names = sorted(p.name for p in (WORK / path.strip("/")).iterdir())
    return ToolMessage(content=str(names), tool_call_id=tool_call_id, name="ls")


@tool
def read_file(file_path: str, tool_call_id: Annotated[str, InjectedToolCallId]) -> ToolMessage:
    """Read a file."""
    side(f"read_file {file_path}")
    target = WORK / file_path.strip("/")
    if not target.exists():
        return ToolMessage(
            content=f"Error: File '{file_path}' not found", tool_call_id=tool_call_id,
            name="read_file", status="error",
        )
    return ToolMessage(
        content=target.read_text(), tool_call_id=tool_call_id, name="read_file",
        artifact={"lines": len(target.read_text().splitlines())},
    )


class Opaque:
    pass


@tool
def snapshot(tool_call_id: Annotated[str, InjectedToolCallId]) -> ToolMessage:
    """A ToolMessage whose artifact is not JSON."""
    side("snapshot")
    return ToolMessage(content="snap", tool_call_id=tool_call_id, artifact=Opaque())


def model(state):
    if any(isinstance(m, ToolMessage) for m in state["messages"]):
        return {"messages": [AIMessage("done")]}
    plan = json.loads(os.environ["AGENT_PLAN"])
    return {"messages": [AIMessage("", tool_calls=[
        {"name": op["tool"], "args": op.get("args", {}), "id": f"call-{i}"}
        for i, op in enumerate(plan)
    ])]}


graph = StateGraph(MessagesState)
graph.add_node("agent", model)
graph.add_node("tools", ToolNode([ls, read_file, snapshot]))
graph.add_edge(START, "agent")
graph.add_conditional_edges("agent", tools_condition)
graph.add_edge("tools", "agent")
APP = graph.compile()

out = []
try:
    result = APP.invoke({"messages": [HumanMessage("review")]})
    for m in result["messages"]:
        if isinstance(m, ToolMessage):
            out.append({
                "type": type(m).__name__, "name": m.name, "content": m.content,
                "status": m.status, "tool_call_id": m.tool_call_id, "artifact": m.artifact,
            })
    out.append({"final": result["messages"][-1].content})
except BaseException as exc:
    out.append({"error": type(exc).__name__})
with open(OUT, "w") as fh:
    json.dump(out, fh, default=lambda o: type(o).__name__)
'''

PLAN = [
    {"tool": "ls", "args": {"path": "/runs"}},
    {"tool": "read_file", "args": {"file_path": "/runs/exp-a/metrics.csv"}},
    {"tool": "read_file", "args": {"file_path": "/runs/missing.csv"}},
]


class Env:
    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.agent = root / "agent.py"
        self.agent.write_text(AGENT)
        work = root / "work"
        (work / "runs" / "exp-a").mkdir(parents=True)
        (work / "runs" / "exp-b").mkdir()
        (work / "runs" / "exp-a" / "metrics.csv").write_text("epoch,loss\n1,2.3\n2,1.9\n")
        self.side = root / "side.log"
        self.out = root / "out.json"
        self.runs = root / "runs"
        self.replays = root / "replays"
        self._mp = monkeypatch
        monkeypatch.setenv("AGENT_SIDE", str(self.side))
        monkeypatch.setenv("AGENT_OUT", str(self.out))
        monkeypatch.setenv("AGENT_WORK", str(work))
        monkeypatch.setenv("LANGSMITH_TRACING", "false")
        for var in ("FORCE_COLOR", "COLORTERM", "NOVA_CAPTURE_LEVEL", "LANGCHAIN_TRACING_V2"):
            monkeypatch.delenv(var, raising=False)

    def capture(self, plan: list[dict[str, Any]]) -> tuple[Path, list[dict[str, Any]]]:
        self._mp.setenv("AGENT_PLAN", json.dumps(plan))
        self._mp.setenv("NOVA_CAPTURE_LEVEL", "forensic")
        result = runner.invoke(
            app, ["capture", "--output-dir", str(self.runs), sys.executable, str(self.agent)]
        )
        self._mp.delenv("NOVA_CAPTURE_LEVEL")
        assert result.exit_code == 0, result.output
        (capsule,) = [p for p in self.runs.iterdir() if (p / "capsule.yaml").exists()]
        captured = json.loads(self.out.read_text())
        self.out.unlink()
        self.side.unlink()
        return capsule, captured

    def records(self, capsule: Path) -> dict[str, list[dict[str, Any]]]:
        out: dict[str, list[dict[str, Any]]] = {}
        for line in (capsule / "tool-calls.jsonl").read_text().splitlines():
            if line.strip():
                rec = json.loads(line)
                out.setdefault(rec["tool_name"], []).append(rec)
        return out

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


def test_a_tool_message_result_is_recorded_as_a_json_envelope(env: Env) -> None:
    capsule, captured = env.capture(PLAN)
    assert {"final": "done"} in captured
    records = env.records(capsule)
    # ToolNode runs the calls of one turn in parallel: records in any order.
    reads = records["read_file"]
    (read_ok,) = [r for r in reads if r["result"]["value"]["status"] == "success"]
    (read_missing,) = [r for r in reads if r is not read_ok]
    for rec in (*records["ls"], read_ok, read_missing):
        jsonschema.validate(rec, _TOOL_SCHEMA)
        ext = rec["extensions"]
        assert ext["io.novafabric.result_codec"] == "json-v1", ext
        assert ext["io.novafabric.langchain_result_kind"] == "tool_message"
        assert "tool_call_id" not in rec["result"]["value"]
    assert read_ok["result"]["value"] == {
        "content": "epoch,loss\n1,2.3\n2,1.9\n", "status": "success",
        "name": "read_file", "artifact": {"lines": 3},
    }
    assert read_missing["result"]["value"]["status"] == "error"


def test_strict_replay_serves_a_real_tool_message_and_never_runs_the_tool(env: Env) -> None:
    capsule, captured = env.capture(PLAN)
    result, observed, cli = env.replay(capsule)
    assert cli.exit_code == 0, cli.output
    assert result["status"] == "success", result.get("error")
    assert env.side_effects == [], "a ToolMessage-returning tool ran during mocked replay"
    # The oracle outside the capsule: the same ToolMessages, field for field --
    # content, status, name, artifact and the tool_call_id of the replayed call.
    assert observed == captured
    assert all(o.get("type") == "ToolMessage" for o in observed if "name" in o)
    assert result["tool_calls_mocked"] == 3
    assert result["tool_calls_live"] == 0
    assert result["replay_contract"]["divergences"] == []


def test_a_tool_message_with_a_non_json_artifact_is_not_servable_and_says_why(
    env: Env,
) -> None:
    capsule, _ = env.capture([{"tool": "snapshot"}])
    (rec,) = env.records(capsule)["snapshot"]
    ext = rec["extensions"]
    assert ext["io.novafabric.result_codec"] == "not-servable"
    assert "the ToolMessage artifact" in ext["io.novafabric.not_servable_reason"]
    assert rec["result"] is None

    result, observed, cli = env.replay(capsule)
    assert cli.exit_code == 1
    assert result["error"]["type"] == "ToolSurfaceNotServed"
    assert any(
        "the ToolMessage artifact" in r
        for r in result["error"]["tool_calls_not_served_by_surface"][
            "langchain_core.tools.BaseTool"
        ]["reasons"]
    )
    assert env.side_effects == []
