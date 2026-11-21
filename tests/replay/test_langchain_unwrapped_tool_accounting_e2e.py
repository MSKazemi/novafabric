"""LangChain / LangGraph tools NOT passed through ``wrap_tools``: capture records
them, mocked replay serves a recorded result by default, and never runs one
without counting it.

The defect this pins (c07 real-workload run, 2026-11-19, workload
``hpc_analyst``): a LangGraph agent with two plain ``@tool`` functions -- one
reads a log file, one writes a JSON diagnosis -- was captured with 3 model
calls and **no** tool calls, and its mocked replay reported ``tool_calls_live:
0`` and a ``faithful`` verdict while strace showed the replayed process read
the log and wrote the diagnosis: the tool bodies ran live, unreported.

Owner decision 2026-11-21 (ADR-0306 amendment r3): the recorded result of an
observed (unwrapped) tool is **served** by default, matched one-to-one like a
wrapped tool and gated as mutation class ``unknown``. A call with no servable
record is refused in a strict replay -- up front when the capsule shows it
(observed records without a payload), at the call otherwise -- and runs live
under ``--permissive`` only with ``--allow-unknown-mutation``.

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


def _not_wrapped_legacy_reason() -> str:
    from novafabric.capture import _tool_codec

    return _tool_codec.LEGACY_NOT_WRAPPED_REASON


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
        # Default capture level: no payload, so not servable -- and the reason
        # says how to fix it (not "wrap_tools": an observed record is servable).
        assert ext["io.novafabric.result_codec"] == "not-servable"
        assert "NOVA_CAPTURE_LEVEL=forensic" in ext["io.novafabric.not_servable_reason"]
        assert "wrap_tools" not in ext["io.novafabric.not_servable_reason"]
    read = next(r for r in records if r["tool_name"] == "read_log")
    assert read["arguments"] == {} and read["result"] is None
    assert read["extensions"]["io.novafabric.arguments_digest"]


def test_a_forensic_capture_makes_an_unwrapped_tool_record_servable(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOVA_CAPTURE_LEVEL", "forensic")
    capsule, _ = env.capture(PLAN)
    records = {r["tool_name"]: r for r in env.records(capsule)}
    read = records["read_log"]
    jsonschema.validate(read, _TOOL_SCHEMA)
    ext = read["extensions"]
    assert ext["io.novafabric.langchain_tool_seam"] == "observed"
    assert ext["io.novafabric.result_codec"] == "json-v1"
    assert "io.novafabric.not_servable_reason" not in ext
    assert read["arguments"] == {"path": LOG}
    assert read["result"]["value"].startswith("slurmstepd: error")


# ── replay: served by default ────────────────────────────────────────────────


@pytest.mark.parametrize("mode", ["invoke", "ainvoke"])
def test_strict_replay_serves_recorded_unwrapped_tool_calls_and_never_runs_their_bodies(
    env: Env, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    monkeypatch.setenv("NOVA_CAPTURE_LEVEL", "forensic")
    monkeypatch.setenv("AGENT_MODE", mode)
    plan = [*PLAN, _call("ping")] if mode == "ainvoke" else PLAN
    capsule, captured = env.capture(plan)
    monkeypatch.delenv("NOVA_CAPTURE_LEVEL")

    result, observed, cli = env.replay(capsule)  # strict
    assert cli.exit_code == 0, cli.output
    assert result["status"] == "success", result.get("error")
    assert env.side_effects == [], "an unwrapped tool body ran during mocked replay"
    assert not (env.work / DIAGNOSIS).exists()
    assert observed == captured  # the served results, by an oracle outside the capsule
    n = len(plan)
    assert result["tool_calls_mocked"] == n
    assert result["tool_calls_live"] == 0
    assert result["tool_calls_available"] == n
    contract = result["replay_contract"]
    assert contract["tool_calls_by_surface"][LC]["mocked"] == n
    assert contract["tool_calls_refused"] == 0
    assert contract["tool_calls_unconsumed"] == 0
    assert contract["divergences"] == []
    assert "tool_calls_not_served_by_surface" not in contract
    assert result["queues_fully_consumed"] is True
    live_check = next(c for c in result["fidelity"]["checks"] if c["name"] == "live_calls")
    assert live_check["equal"] is True

    # --dry-run predicts it: served, nothing refused.
    dry, _, cli = env.replay(capsule, "--dry-run")
    assert cli.exit_code == 0, cli.output
    report = next(env.replays.glob(f"{dry['replay_id']}/dry_run_report.txt")).read_text()
    assert not report.startswith("REFUSED:")
    assert "[MOCK] read_log" in report
    assert "[MOCK] write_diagnosis" in report


def test_an_unmatched_unwrapped_call_is_refused_strict_before_its_body_runs(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOVA_CAPTURE_LEVEL", "forensic")
    capsule, _ = env.capture(PLAN)
    monkeypatch.delenv("NOVA_CAPTURE_LEVEL")
    # The replayed run asks for a log the capsule never recorded.
    monkeypatch.setenv("AGENT_PLAN", json.dumps([_call("read_log", path="logs/other.err")]))
    before = set(env.replays.iterdir()) if env.replays.exists() else set()
    cli = runner.invoke(app, ["replay", str(capsule), "-o", str(env.replays)])
    (replay_dir,) = set(env.replays.iterdir()) - before
    result = yaml.safe_load((replay_dir / "replay_result.yaml").read_text())
    jsonschema.validate(result, _RESULT_SCHEMA)
    assert env.side_effects == [], "an unmatched unwrapped tool ran live in a strict replay"
    assert cli.exit_code == 1, cli.output
    assert result["status"] == "failure"
    assert "tool_call_unmatched" in _kinds(result)
    assert result["tool_calls_live"] == 0


# ── replay: not servable, knowable up front ──────────────────────────────────


def test_strict_replay_refuses_to_start_when_unwrapped_tool_results_were_not_recorded(
    env: Env,
) -> None:
    capsule, _ = env.capture(PLAN)  # default level: no payloads
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
    assert any("NOVA_CAPTURE_LEVEL=forensic" in r for r in by_surface[LC]["reasons"])
    message = result["error"]["message"]
    assert "capture again" in message and "NOVA_CAPTURE_LEVEL=forensic" in message
    assert "--permissive" in message and "--allow-unknown-mutation" in message

    # --dry-run predicts it exactly, naming each call.
    dry, _, cli = env.replay(capsule, "--dry-run")
    assert cli.exit_code == 1, cli.output
    assert dry["error"]["type"] == "ToolSurfaceNotServed"
    report = next(env.replays.glob(f"{dry['replay_id']}/dry_run_report.txt")).read_text()
    assert report.startswith("REFUSED:")
    assert "[NOT SERVED] LangChain tool call (read_log)" in report
    assert "[NOT SERVED] LangChain tool call (write_diagnosis)" in report
    assert "[DENY (replay refuses to start)] read_log" in report


def test_permissive_runs_unservable_unwrapped_tools_live_only_with_allow_unknown_mutation(
    env: Env,
) -> None:
    capsule, captured = env.capture(PLAN)  # default level: not servable

    # --permissive alone: the ladder does not permit `unknown`, so each call is
    # refused before its body runs (ADR-0306 Q3, as MCP).
    result, observed, cli = env.replay(capsule, "--permissive")
    assert env.side_effects == [], "--permissive alone ran an unwrapped tool live"
    assert not (env.work / DIAGNOSIS).exists()
    assert result["tool_calls_live"] == 0
    assert result["replay_contract"]["tool_calls_refused"] >= 1
    assert _kinds(result)[0] == "tool_surface_not_served"
    assert any(o.get("error") for o in observed)

    # With the ladder flag: live, counted and named.
    result, observed, cli = env.replay(capsule, "--permissive", "--allow-unknown-mutation")
    assert observed == captured
    # ToolNode runs the two calls of one model turn in parallel: any order.
    assert sorted(env.side_effects) == [f"read_log {LOG}", "write_diagnosis 42819"]
    assert (env.work / DIAGNOSIS).exists()
    assert cli.exit_code == 0, cli.output
    assert result["tool_calls_live"] == 2
    assert result["tool_calls_mocked"] == 0
    assert result["tool_calls_available"] == 0
    contract = result["replay_contract"]
    assert contract["tool_calls_by_surface"][LC]["live"] == 2
    assert _live_by_tool(result) == {"read_log": 1, "write_diagnosis": 1}
    assert result["queues_fully_consumed"] is True
    assert _kinds(result)[0] == "tool_surface_not_served"
    assert result["fidelity"]["verdict"] != "faithful"
    assert "[LIVE] tool call (langchain_core.tools.BaseTool: read_log)" in cli.output
    assert "[LIVE] tool call (langchain_core.tools.BaseTool: write_diagnosis)" in cli.output


# ── replay: capsules written before this change ──────────────────────────────


def test_an_r2_observed_record_with_its_payload_is_served(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-2 capture marked every observed record not-servable with exactly the
    "not passed through wrap_tools" reason, even at ``forensic`` with the value
    kept: that reason was policy, not data, so the record is servable now."""
    monkeypatch.setenv("NOVA_CAPTURE_LEVEL", "forensic")
    capsule, captured = env.capture(PLAN)
    monkeypatch.delenv("NOVA_CAPTURE_LEVEL")
    records = env.records(capsule)
    for rec in records:
        rec["extensions"]["io.novafabric.result_codec"] = "not-servable"
        rec["extensions"]["io.novafabric.not_servable_reason"] = _not_wrapped_legacy_reason()
    env.write_records(capsule, records)

    result, observed, cli = env.replay(capsule)
    assert cli.exit_code == 0, cli.output
    assert env.side_effects == []
    assert observed == captured
    assert result["tool_calls_mocked"] == 2


def test_an_r2_observed_record_without_payload_is_refused_and_says_capture_again(
    env: Env,
) -> None:
    from novafabric.capture.hooks._python_tool import PAYLOADS_OFF_REASON

    capsule, _ = env.capture(PLAN)
    records = env.records(capsule)
    for rec in records:
        rec["extensions"]["io.novafabric.not_servable_reason"] = (
            f"{_not_wrapped_legacy_reason()}; {PAYLOADS_OFF_REASON}"
        )
    env.write_records(capsule, records)

    result, _, cli = env.replay(capsule)
    assert cli.exit_code == 1
    assert result["error"]["type"] == "ToolSurfaceNotServed"
    reasons = result["error"]["tool_calls_not_served_by_surface"][LC]["reasons"]
    # The legacy policy text is gone; what is left is the payloads-off reason and,
    # since ADR-0306 r3b, why the model-request echo could not recover it (this
    # capsule's scripted model records no model calls).
    assert len(reasons) == 1 and reasons[0].startswith(PAYLOADS_OFF_REASON)
    assert "no recorded model call holds request messages" in reasons[0]
    assert _not_wrapped_legacy_reason() not in reasons[0]
    assert "capture again" in result["error"]["message"]


# ── replay: not knowable up front (the c07 case) ─────────────────────────────


@pytest.mark.parametrize("mode", ["invoke", "ainvoke"])
def test_a_capsule_without_tool_records_refuses_strict_and_counts_permissive(
    env: Env, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """The hpc_analyst shape: model calls recorded and served, tool calls not
    recorded (a capsule from before the observer existed). Nothing tells the
    replay up front; a strict replay refuses each tool call at the call (before
    its body), and ``--permissive --allow-unknown-mutation`` runs them live,
    counted and named."""
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
    assert env.side_effects == [], "a strict replay ran an unrecorded tool body"
    assert not (env.work / DIAGNOSIS).exists()
    assert cli.exit_code == 1, cli.output
    assert result["status"] == "failure"
    assert "tool_call_unmatched" in _kinds(result)
    assert result["tool_calls_live"] == 0
    assert result["fidelity"]["verdict"] != "faithful"

    result, observed, cli = env.replay(capsule, "--permissive", "--allow-unknown-mutation")
    assert observed == captured
    assert result["model_calls_mocked"] == 3
    assert env.side_effects == [f"read_log {LOG}", "write_diagnosis 42819"]
    assert result["tool_calls_live"] == 2, result
    assert _live_by_tool(result) == {"read_log": 1, "write_diagnosis": 1}
    assert result["fidelity"]["verdict"] != "faithful"
    live_check = next(c for c in result["fidelity"]["checks"] if c["name"] == "live_calls")
    assert live_check["equal"] is False
    assert "[LIVE] tool call (langchain_core.tools.BaseTool: read_log)" in cli.output
    assert "2 live" in cli.output


# ── wrapped and unwrapped tools together; one record per call ────────────────


def test_wrapped_and_unwrapped_tools_are_both_served_one_record_per_call(
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

    result, observed, _ = env.replay(capsule)
    assert observed == captured
    assert env.side_effects == [], "a tool body ran during mocked replay"
    assert result["tool_calls_mocked"] == 2
    assert result["tool_calls_live"] == 0
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


# ── default capture level: results recovered from the next model request ────


def _openai_hpc_capsule(env: Env, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Any]:
    pytest.importorskip("openai")
    pytest.importorskip("httpx")
    from _mocked_replay_agent import openai_body

    monkeypatch.setenv("AGENT_MODEL", "openai")
    monkeypatch.setenv("AGENT_CANNED", json.dumps([
        openai_body(tool_calls=[("call_R", "read_log", {"path": LOG})], rid="r1"),
        openai_body(tool_calls=[
            ("call_W", "write_diagnosis", {"job_id": "42819", "severity": "HIGH"}),
        ], rid="r2"),
        openai_body(content="diagnosed", rid="r3"),
    ]))
    return env.capture([])  # default level: tool results not kept, digests are


def test_a_default_level_capsule_is_served_from_the_model_request_echo(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The c08 hpc_analyst case: each tool result is recorded only as a digest,
    but the next model request carries it back to the model. A strict replay
    serves it from there, because its digest matches the recorded one."""
    capsule, captured = _openai_hpc_capsule(env, monkeypatch)
    records = env.records(capsule)
    assert {r["tool_name"] for r in records} == {"read_log", "write_diagnosis"}
    assert all(r["result"] is None for r in records)  # nothing new was stored

    result, observed, cli = env.replay(capsule)  # strict, AGENT_CANNED unset
    assert cli.exit_code == 0, cli.output
    assert result["status"] == "success", result.get("error")
    assert env.side_effects == [], "a tool body ran during mocked replay"
    assert not (env.work / DIAGNOSIS).exists()
    assert observed == captured
    assert result["model_calls_mocked"] == 3
    assert result["tool_calls_mocked"] == 2
    assert result["tool_calls_live"] == 0
    contract = result["replay_contract"]
    assert contract["tool_calls_mocked_by_source"] == {"model-request-echo": 2}
    assert "2 of the served tool results came from the next recorded model request" in (
        cli.output.replace("\n", " ")
    )
    assert contract["divergences"] == []

    dry, _, cli = env.replay(capsule, "--dry-run")
    assert cli.exit_code == 0, cli.output
    report = next(env.replays.glob(f"{dry['replay_id']}/dry_run_report.txt")).read_text()
    assert "[MOCK] read_log" in report
    assert "the next model request's echo, matched to the recorded digest" in report


def test_an_echo_that_does_not_match_the_recorded_digest_is_never_served(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    capsule, _ = _openai_hpc_capsule(env, monkeypatch)
    records = env.records(capsule)
    for rec in records:
        if rec["tool_name"] == "read_log":
            rec["extensions"]["io.novafabric.result_digest"] = "sha256:" + "0" * 64
    env.write_records(capsule, records)

    result, observed, cli = env.replay(capsule)
    assert cli.exit_code == 1, cli.output
    assert result["error"]["type"] == "ToolSurfaceNotServed"
    by_surface = result["error"]["tool_calls_not_served_by_surface"][LC]
    assert by_surface["tools"] == ["read_log"]  # write_diagnosis is still recoverable
    assert "NOVA_CAPTURE_LEVEL=forensic" in result["error"]["message"]
    assert env.side_effects == []
