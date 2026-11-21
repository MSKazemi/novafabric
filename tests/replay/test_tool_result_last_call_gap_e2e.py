"""The last-call echo gap (ADR-0306 r3b): below ``forensic`` a tool result is
served only when a later recorded model request echoes it under the recorded
digest. The result of a run's *last* tool call never goes back to a model, so
nothing in the capsule is a digest-checked source for it.

It is not guessed. The capsule's ``replayability.json``, the strict refusal
(``ToolSurfaceNotServed``) and ``--dry-run`` name the case precisely ("result
never sent to a model") and say what works: capture at ``forensic``, or replay
``--permissive --allow-unknown-mutation``. An earlier call of the same run,
whose result the next request did echo, is still served.

Checked and rejected as sources (see the ADR text): the final graph state is
not in a capsule below ``forensic`` (the LangGraph adapter keeps only its
digest), and ``outputs/stdout.txt`` holds the result only if the workload
happened to print it verbatim.

The workload is a real ``StateGraph`` whose route ends the run right after the
``finish`` tool, with the real ``openai`` SDK over an ``httpx.MockTransport``.
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

LC = "langchain_core.tools.BaseTool"
NEVER_SENT = "result never sent to a model"

AGENT = '''
import json, os

import httpx
from openai import OpenAI
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition

SIDE = os.environ["AGENT_SIDE"]
OUT = os.environ["AGENT_OUT"]
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


@tool
def read_log(job_id: str) -> str:
    """Read a job log."""
    side(f"read_log {job_id}")
    return f"job {job_id}: oom_kill event"


@tool
def finish(summary: str) -> str:
    """Record the diagnosis and end the run."""
    side(f"finish {summary}")
    return f"diagnosis saved: {summary}"


TOOLS = [read_log, finish]
SPECS = [{"type": "function", "function": {
    "name": t.name, "description": t.description,
    "parameters": t.tool_call_schema.model_json_schema()}} for t in TOOLS]


def to_openai(m):
    if isinstance(m, HumanMessage):
        return {"role": "user", "content": m.content}
    if isinstance(m, ToolMessage):
        return {"role": "tool", "tool_call_id": m.tool_call_id, "content": m.content}
    return {"role": "assistant", "content": m.content or None, "tool_calls": [
        {"id": c["id"], "type": "function",
         "function": {"name": c["name"], "arguments": json.dumps(c["args"])}}
        for c in m.tool_calls] or None}


def agent(state):
    r = CLIENT.chat.completions.create(
        model="gpt-4o", tools=SPECS, messages=[to_openai(m) for m in state["messages"]])
    msg = r.choices[0].message
    calls = [{"name": c.function.name, "args": json.loads(c.function.arguments),
              "id": c.id} for c in (msg.tool_calls or [])]
    return {"messages": [AIMessage(msg.content or "", tool_calls=calls)]}


def after_tools(state):
    # The run ends right after `finish`: its result never goes back to the model.
    last = state["messages"][-1]
    return END if getattr(last, "name", None) == "finish" else "agent"


graph = StateGraph(MessagesState)
graph.add_node("agent", agent)
graph.add_node("tools", ToolNode(TOOLS))
graph.add_edge(START, "agent")
graph.add_conditional_edges("agent", tools_condition)
graph.add_conditional_edges("tools", after_tools)
APP = graph.compile()


def main():
    out = []
    try:
        result = APP.invoke({"messages": [HumanMessage("diagnose job 7")]})
        for m in result["messages"]:
            if isinstance(m, ToolMessage):
                out.append({"name": m.name, "response": m.content})
    except BaseException as exc:  # the workload swallows every failure
        out.append({"error": type(exc).__name__})
    with open(OUT, "w") as fh:
        json.dump(out, fh)


main()
'''


def _canned() -> list[dict[str, Any]]:
    from _mocked_replay_agent import openai_body

    return [
        openai_body(tool_calls=[("call_R", "read_log", {"job_id": "7"})], rid="r1"),
        openai_body(tool_calls=[("call_F", "finish", {"summary": "OOM"})], rid="r2"),
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
        for var in ("FORCE_COLOR", "COLORTERM", "AGENT_CANNED", "NOVA_CAPTURE_LEVEL",
                    "LANGCHAIN_TRACING_V2", "LANGSMITH_API_KEY", "LANGCHAIN_API_KEY"):
            monkeypatch.delenv(var, raising=False)

    def capture(self, level: str | None = None) -> tuple[Path, list[Any], str]:
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
        return capsule, captured, result.output

    def replay(self, capsule: Path, *flags: str) -> tuple[dict[str, Any], Any, Path]:
        before = set(self.replays.iterdir()) if self.replays.exists() else set()
        cli = runner.invoke(app, ["replay", str(capsule), "-o", str(self.replays), *flags])
        (replay_dir,) = set(self.replays.iterdir()) - before
        result = yaml.safe_load((replay_dir / "replay_result.yaml").read_text())
        jsonschema.validate(result, _RESULT_SCHEMA)
        if self.out.exists():
            self.out.unlink()
        return result, cli, replay_dir

    @property
    def side_effects(self) -> list[str]:
        return self.side.read_text().splitlines() if self.side.exists() else []


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Env:
    return Env(tmp_path, monkeypatch)


def test_the_capture_time_summary_names_the_last_call_gap(env: Env) -> None:
    capsule, captured, output = env.capture()
    assert [o["name"] for o in captured] == ["read_log", "finish"]
    doc = json.loads((capsule / "replayability.json").read_text())
    assert doc["strict_mocked"] == "refuse"
    (reason,) = [r for r in doc["reasons"] if r["code"] == "tool_surface_not_served"]
    assert reason["count"] == 1 and reason["tools"] == ["finish"]
    assert reason["result_never_sent_to_a_model"] == ["finish"]
    assert NEVER_SENT in reason["summary"]
    assert NEVER_SENT in reason["detail"]
    assert "NOVA_CAPTURE_LEVEL=forensic" in reason["detail"]
    assert doc["refusal"]["type"] == "ToolSurfaceNotServed"
    assert NEVER_SENT in " ".join(output.split())


def test_strict_replay_refuses_and_names_the_last_call(env: Env) -> None:
    capsule, _, _ = env.capture()
    result, cli, _ = env.replay(capsule)
    assert cli.exit_code == 1, cli.output
    assert env.side_effects == [], "the workload must not run"
    error = result["error"]
    assert error["type"] == "ToolSurfaceNotServed"
    by_surface = error["tool_calls_not_served_by_surface"][LC]
    # read_log's result came back in the next request and is recoverable;
    # finish's never went to a model.
    assert by_surface["tools"] == ["finish"]
    (why,) = by_surface["reasons"]
    assert NEVER_SENT in why
    assert "NOVA_CAPTURE_LEVEL=forensic" in why
    assert "--permissive --allow-unknown-mutation" in why
    assert NEVER_SENT in error["message"]

    dry, cli, replay_dir = env.replay(capsule, "--dry-run")
    assert cli.exit_code == 1, cli.output
    report = (replay_dir / "dry_run_report.txt").read_text()
    line = next(x for x in report.splitlines() if "[NOT SERVED]" in x)
    assert "(finish)" in line and NEVER_SENT in line


def test_permissive_runs_only_the_last_call_live_with_the_ladder_flag(env: Env) -> None:
    capsule, captured, _ = env.capture()
    result, cli, _ = env.replay(capsule, "--permissive", "--allow-unknown-mutation")
    assert cli.exit_code == 0, cli.output
    assert env.side_effects == ["finish OOM"]  # read_log served, finish ran live
    assert result["tool_calls_mocked"] == 1
    assert result["tool_calls_live"] == 1


def test_a_forensic_capture_closes_the_gap(env: Env) -> None:
    capsule, captured, _ = env.capture(level="forensic")
    doc = json.loads((capsule / "replayability.json").read_text())
    assert doc["strict_mocked"] == "start"
    result, cli, _ = env.replay(capsule)
    assert cli.exit_code == 0, cli.output
    assert result["status"] == "success", result.get("error")
    assert env.side_effects == []
    assert result["tool_calls_mocked"] == 2
