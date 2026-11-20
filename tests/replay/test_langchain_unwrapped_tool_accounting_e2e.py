"""LangChain / LangGraph tools NOT passed through ``wrap_tools``: capture records
them, and mocked replay never runs one without counting it.

The defect this pins (c07 real-workload run, 2026-11-19, workload
``hpc_analyst``): a LangGraph agent with two plain ``@tool`` functions -- one
reads a log file, one writes a JSON diagnosis -- was captured with 3 model
calls and **no** tool calls, and its mocked replay reported ``tool_calls_live:
0`` and a ``faithful`` verdict while strace showed the replayed process read
the log and wrote the diagnosis: the tool bodies ran live, unreported.

The workload here has the same shape (``ToolNode`` over plain ``@tool``
functions that read and write files), with an offline scripted model
(``AGENT_MODEL=plan``) or the real ``openai`` SDK over an
``httpx.MockTransport`` (``AGENT_MODEL=openai``: the model calls are recorded
and served). No network, no API key.
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

AGENT = '''
import asyncio, json, os
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.graph import START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition

SIDE = os.environ["AGENT_SIDE"]
OUT = os.environ["AGENT_OUT"]
WORK = Path(os.environ["AGENT_WORK"])
MODE = os.environ.get("AGENT_MODE", "invoke")


def side(text):
    with open(SIDE, "a") as fh:
        fh.write(text + "\\n")


@tool
def read_log(path: str) -> str:
    """Read a job error log."""
    side(f"read_log {path}")
    return (WORK / path).read_text()


@tool
def write_diagnosis(job_id: str, severity: str) -> str:
    """Save a JSON diagnosis for the failed job."""
    side(f"write_diagnosis {job_id}")
    out = WORK / "output" / f"job-{job_id}-diagnosis.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({"job_id": job_id, "severity": severity}))
    return f"saved {out.name}"


@tool
async def ping() -> str:
    """Ping, asynchronously."""
    side("ping")
    return "pong"


@tool
def lookup(order_id: str) -> dict:
    """Look up an order (passed through wrap_tools when AGENT_WRAP names it)."""
    side(f"lookup {order_id}")
    return {"status": "shipped", "id": order_id}


TOOLS = [read_log, write_diagnosis, ping, lookup]
WRAP = set(json.loads(os.environ.get("AGENT_WRAP", "[]")))
if WRAP:
    from novafabric.adapters.langchain_tools import wrap_tools

    TOOLS = [wrap_tools([t])[0] if t.name in WRAP else t for t in TOOLS]


def _plan_model(state):
    if any(isinstance(m, ToolMessage) for m in state["messages"]):
        return {"messages": [AIMessage("done")]}
    plan = json.loads(os.environ["AGENT_PLAN"])
    return {"messages": [AIMessage("", tool_calls=[
        {"name": op["tool"], "args": op.get("args", {}), "id": f"call-{i}"}
        for i, op in enumerate(plan)
    ])]}


def _openai_model():
    import httpx
    from openai import OpenAI

    canned = json.loads(os.environ.get("AGENT_CANNED") or "null")
    served = [0]

    def transport(request):
        if canned is None:
            raise RuntimeError("network reached during replay")
        body = canned[served[0]]
        served[0] += 1
        return httpx.Response(200, json=body)

    client = OpenAI(api_key="sk-test", max_retries=0,
                    http_client=httpx.Client(transport=httpx.MockTransport(transport)))
    specs = [{"type": "function", "function": {
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

    def node(state):
        r = client.chat.completions.create(
            model="gpt-4o", tools=specs,
            messages=[to_openai(m) for m in state["messages"]])
        msg = r.choices[0].message
        calls = [{"name": c.function.name, "args": json.loads(c.function.arguments),
                  "id": c.id} for c in (msg.tool_calls or [])]
        return {"messages": [AIMessage(msg.content or "", tool_calls=calls)]}

    return node


graph = StateGraph(MessagesState)
graph.add_node(
    "agent", _openai_model() if os.environ.get("AGENT_MODEL") == "openai" else _plan_model
)
graph.add_node("tools", ToolNode(TOOLS))
graph.add_edge(START, "agent")
graph.add_conditional_edges("agent", tools_condition)
graph.add_edge("tools", "agent")
APP = graph.compile()


def main():
    out = []
    try:
        state = {"messages": [HumanMessage("diagnose")]}
        result = asyncio.run(APP.ainvoke(state)) if MODE == "ainvoke" else APP.invoke(state)
        for m in result["messages"]:
            if isinstance(m, ToolMessage):
                out.append({"name": m.name, "response": m.content, "status": m.status})
        out.append({"final": result["messages"][-1].content})
    except BaseException as exc:  # the workload swallows every failure
        out.append({"error": type(exc).__name__})
    with open(OUT, "w") as fh:
        json.dump(out, fh)


main()
'''

LOG = "logs/job-42819-oom.err"
DIAGNOSIS = "output/job-42819-diagnosis.json"


class Env:
    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.root = root
        self.agent = root / "agent.py"
        self.agent.write_text(AGENT)
        self.work = root / "work"
        (self.work / "logs").mkdir(parents=True)
        (self.work / LOG).write_text("slurmstepd: error: Detected 1 oom_kill event\n")
        self.side = root / "side.log"
        self.out = root / "out.json"
        self.runs = root / "runs"
        self.replays = root / "replays"
        self._mp = monkeypatch
        monkeypatch.setenv("AGENT_SIDE", str(self.side))
        monkeypatch.setenv("AGENT_OUT", str(self.out))
        monkeypatch.setenv("AGENT_WORK", str(self.work))
        monkeypatch.setenv("LANGSMITH_TRACING", "false")
        for var in (
            "FORCE_COLOR", "COLORTERM", "AGENT_MODE", "AGENT_MODEL", "AGENT_CANNED",
            "AGENT_WRAP", "NOVA_CAPTURE_LEVEL", "LANGCHAIN_TRACING_V2",
            "LANGSMITH_API_KEY", "LANGCHAIN_API_KEY",
        ):
            monkeypatch.delenv(var, raising=False)

    def capture(self, plan: list[dict[str, Any]]) -> tuple[Path, list[dict[str, Any]]]:
        self._mp.setenv("AGENT_PLAN", json.dumps(plan))
        result = runner.invoke(
            app, ["capture", "--output-dir", str(self.runs), sys.executable, str(self.agent)]
        )
        assert result.exit_code == 0, result.output
        (capsule,) = [p for p in self.runs.iterdir() if (p / "capsule.yaml").exists()]
        captured = json.loads(self.out.read_text())
        self.out.unlink()
        self.reset_side_effects()
        self._mp.delenv("AGENT_CANNED", raising=False)
        return capsule, captured

    def reset_side_effects(self) -> None:
        if self.side.exists():
            self.side.unlink()
        diagnosis = self.work / DIAGNOSIS
        if diagnosis.exists():
            diagnosis.unlink()

    def records(self, capsule: Path) -> list[dict[str, Any]]:
        path = capsule / "tool-calls.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def write_records(self, capsule: Path, records: list[dict[str, Any]]) -> None:
        (capsule / "tool-calls.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in records)
        )

    def replay(
        self, capsule: Path, *flags: str
    ) -> tuple[dict[str, Any], list[dict[str, Any]], Any]:
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


def _call(tool: str, **args: Any) -> dict[str, Any]:
    return {"tool": tool, "args": args}


PLAN = [_call("read_log", path=LOG), _call("write_diagnosis", job_id="42819", severity="HIGH")]


def _kinds(result: dict[str, Any]) -> list[str]:
    return [d["kind"] for d in result["replay_contract"]["divergences"]]


def _live_by_tool(result: dict[str, Any]) -> dict[str, int]:
    return {
        e["tool_name"]: e["count"]
        for e in result["replay_contract"]["tool_calls_live_by_tool"]
        if e["surface"] == LC
    }


# ── capture ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("mode", ["invoke", "ainvoke"])
def test_capture_records_langchain_tools_that_were_not_wrapped(
    env: Env, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    monkeypatch.setenv("AGENT_MODE", mode)
    plan = [*PLAN, _call("ping")] if mode == "ainvoke" else PLAN
    capsule, captured = env.capture(plan)
    assert {"final": "done"} in captured
    records = env.records(capsule)
    assert sorted(r["tool_name"] for r in records) == sorted(op["tool"] for op in plan)
    for rec in records:
        jsonschema.validate(rec, _TOOL_SCHEMA)
        ext = rec["extensions"]
        assert rec["transport"] == "python"
        assert ext["io.novafabric.tool_surface"] == "langchain.tool"
        assert ext["io.novafabric.langchain_tool_seam"] == "observed"
        assert rec["mutation_class"] == "unknown"
        assert ext["io.novafabric.langchain_tool_call_id"].startswith("call-")
        # Never served: replay serves only tools declared through wrap_tools.
        assert ext["io.novafabric.result_codec"] == "not-servable"
        assert "wrap_tools" in ext["io.novafabric.not_servable_reason"]
    # Default capture level: digests only, no payload.
    read = next(r for r in records if r["tool_name"] == "read_log")
    assert read["arguments"] == {} and read["result"] is None
    assert read["extensions"]["io.novafabric.arguments_digest"]


# ── replay: knowable up front ────────────────────────────────────────────────


def test_strict_replay_refuses_to_start_when_unwrapped_tool_calls_were_recorded(
    env: Env,
) -> None:
    capsule, _ = env.capture(PLAN)
    result, observed, cli = env.replay(capsule)
    assert env.side_effects == [], "the workload must not run"
    assert not (env.work / DIAGNOSIS).exists()
    assert observed == []
    assert cli.exit_code == 1, cli.output
    assert result["status"] == "aborted"
    assert result["error"]["type"] == "ToolSurfaceNotServed"
    by_surface = result["error"]["tool_calls_not_served_by_surface"]
    assert by_surface[LC]["count"] == 2
    assert by_surface[LC]["tools"] == ["read_log", "write_diagnosis"]
    assert "wrap_tools" in result["error"]["message"]
    assert "--permissive" in result["error"]["message"]

    # --dry-run predicts it exactly, naming each call.
    dry, _, cli = env.replay(capsule, "--dry-run")
    assert cli.exit_code == 1, cli.output
    assert dry["error"]["type"] == "ToolSurfaceNotServed"
    report = next(env.replays.glob(f"{dry['replay_id']}/dry_run_report.txt")).read_text()
    assert report.startswith("REFUSED:")
    assert "[LIVE] LangChain tool call (read_log)" in report
    assert "[LIVE] LangChain tool call (write_diagnosis)" in report


def test_permissive_runs_unwrapped_tools_live_and_counts_and_names_them(env: Env) -> None:
    capsule, captured = env.capture(PLAN)
    result, observed, cli = env.replay(capsule, "--permissive")
    assert observed == captured
    # ToolNode runs the two calls of one model turn in parallel: any order.
    assert sorted(env.side_effects) == [f"read_log {LOG}", "write_diagnosis 42819"]
    assert (env.work / DIAGNOSIS).exists()
    assert cli.exit_code == 0, cli.output
    assert result["tool_calls_live"] == 2
    assert result["tool_calls_mocked"] == 0
    assert result["tool_calls_available"] == 0  # never served, so never available
    contract = result["replay_contract"]
    assert contract["tool_calls_by_surface"][LC]["live"] == 2
    assert _live_by_tool(result) == {"read_log": 1, "write_diagnosis": 1}
    # The recorded calls were requested (consumed), so nothing is left over.
    assert contract["tool_calls_unconsumed"] == 0
    assert result["queues_fully_consumed"] is True
    assert _kinds(result)[0] == "tool_surface_not_served"
    assert result["fidelity"]["verdict"] != "faithful"
    assert "[LIVE] tool call (langchain_core.tools.BaseTool: read_log)" in cli.output
    assert "[LIVE] tool call (langchain_core.tools.BaseTool: write_diagnosis)" in cli.output


# ── replay: not knowable up front (the c07 case) ─────────────────────────────


@pytest.mark.parametrize("mode", ["invoke", "ainvoke"])
def test_a_capsule_without_tool_records_still_counts_every_live_tool_body(
    env: Env, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """The hpc_analyst shape: model calls recorded and served, tool calls not
    recorded (a capsule from before this capture existed). The tool bodies run
    live; the report must say so."""
    pytest.importorskip("openai")
    pytest.importorskip("httpx")
    from _mocked_replay_agent import openai_body

    monkeypatch.setenv("AGENT_MODE", mode)
    monkeypatch.setenv("AGENT_MODEL", "openai")
    monkeypatch.setenv("AGENT_CANNED", json.dumps([
        openai_body(tool_calls=[("call_R", "read_log", {"path": LOG})], rid="r1"),
        openai_body(tool_calls=[
            ("call_W", "write_diagnosis", {"job_id": "42819", "severity": "HIGH"}),
        ], rid="r2"),
        openai_body(content="diagnosed", rid="r3"),
    ]))
    capsule, captured = env.capture([])
    assert captured[-1] == {"final": "diagnosed"}
    env.write_records(capsule, [])  # as the c07 capsule: no tool records

    result, observed, cli = env.replay(capsule)  # strict, AGENT_CANNED unset
    assert observed == captured
    assert result["model_calls_mocked"] == 3
    assert env.side_effects == [f"read_log {LOG}", "write_diagnosis 42819"]
    assert (env.work / DIAGNOSIS).exists()
    # Before the fix: tool_calls_live 0 and a faithful verdict.
    assert result["tool_calls_live"] == 2, result
    assert _live_by_tool(result) == {"read_log": 1, "write_diagnosis": 1}
    assert result["fidelity"]["verdict"] != "faithful"
    live_check = next(c for c in result["fidelity"]["checks"] if c["name"] == "live_calls")
    assert live_check["equal"] is False
    assert "[LIVE] tool call (langchain_core.tools.BaseTool: read_log)" in cli.output
    assert "2 live" in cli.output


# ── wrapped tools keep being served; one record per call ─────────────────────


def test_wrapped_tools_are_served_while_unwrapped_ones_run_live_and_are_counted(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOVA_CAPTURE_LEVEL", "forensic")
    monkeypatch.setenv("AGENT_WRAP", json.dumps(["lookup"]))
    plan = [_call("lookup", order_id="o-1"), _call("read_log", path=LOG)]
    capsule, captured = env.capture(plan)
    records = env.records(capsule)
    # Exactly one record per call: the observer steps aside for a wrapped tool.
    assert sorted(r["tool_name"] for r in records) == ["lookup", "read_log"]
    seams = {r["tool_name"]: r["extensions"].get("io.novafabric.langchain_tool_seam")
             for r in records}
    assert seams == {"lookup": "wrap_tools", "read_log": "observed"}

    result, observed, _ = env.replay(capsule, "--permissive")
    assert observed == captured
    assert env.side_effects == [f"read_log {LOG}"], "the wrapped tool's body ran"
    assert result["tool_calls_mocked"] == 1
    assert result["tool_calls_live"] == 1
    assert _live_by_tool(result) == {"read_log": 1}
    assert result["queues_fully_consumed"] is True


def test_an_allow_false_override_stops_an_unwrapped_tool_before_its_body_runs(
    env: Env,
) -> None:
    capsule, _ = env.capture(PLAN)
    (capsule / "replay.yaml").write_text(yaml.safe_dump({
        "schema_version": "0.1.0",
        "tool_overrides": [{"tool_name": "write_diagnosis", "allow": False}],
    }))
    result, observed, _ = env.replay(capsule, "--permissive", "--allow-unknown-mutation")
    assert "write_diagnosis 42819" not in env.side_effects
    assert not (env.work / DIAGNOSIS).exists(), "`allow: false` must never run the tool live"
    assert result["replay_contract"]["tool_calls_refused"] == 1
    assert _live_by_tool(result) == {"read_log": 1}
    assert any(o.get("error") for o in observed)
