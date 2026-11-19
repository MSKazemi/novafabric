"""ADR-0306 slice 3 (LangChain / LangGraph): tool calls round-trip through real
``nova capture`` and mocked ``nova replay`` subprocesses.

The workload is a real LangGraph ``StateGraph`` -- an agent node, LangGraph's
prebuilt ``tools_condition`` and ``ToolNode`` -- with ``@tool`` functions and a
``BaseTool`` subclass routed through ``langchain_tools.wrap_tools``. The agent
node is either an offline scripted model (``AGENT_MODEL=plan``: the tool calls
come from ``AGENT_PLAN``, so a replay can ask for calls the capsule never saw)
or the real ``openai`` SDK over an ``httpx.MockTransport`` (``AGENT_MODEL=openai``:
the model calls are recorded and served too). No network, no API key.

The seam is a generated subclass of each tool (not a LangChain API), so these
tests are also its red-green proof: a served call leaves the side-effect log
empty, and every fail-closed case refuses *before* the tool body runs.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import jsonschema
import pytest
import yaml
from _mocked_replay_agent import assert_replay_outputs_hold_no_value
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

#: Never appears in source the replay reads; checked for in the event log/result.
SENTINEL = "SENTINEL-lc-41d9"

AGENT = '''
import asyncio, json, os
from typing import Annotated

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import BaseTool, InjectedToolCallId, tool
from langgraph.graph import START, MessagesState, StateGraph
from langgraph.prebuilt import InjectedState, ToolNode, tools_condition

from novafabric.adapters.langchain_tools import wrap_tools
from novafabric.capture import record

SIDE = os.environ["AGENT_SIDE"]
OUT = os.environ["AGENT_OUT"]
TAG = os.environ.get("AGENT_TAG", "")
MODE = os.environ.get("AGENT_MODE", "invoke")
COUNT = {"n": 0}


def side(text):
    with open(SIDE, "a") as fh:
        fh.write(text + "\\n")


@tool
def lookup(order_id: str) -> dict:
    """Look up an order."""
    side(f"lookup {order_id}")
    return {"status": "shipped", "id": order_id, "tag": TAG}


@tool
async def ping() -> str:
    """Ping, asynchronously."""
    side("ping")
    return "pong-" + TAG


@tool
def counter(key: str) -> dict:
    """Count calls."""
    side(f"counter {key}")
    COUNT["n"] += 1
    return {"key": key, "n": COUNT["n"], "tag": TAG}


@tool
def stateful(x: int, state: Annotated[dict, InjectedState],
             tool_call_id: Annotated[str, InjectedToolCallId]) -> str:
    """Receives injected state the model never sends."""
    side(f"stateful {x}")
    return f"stateful-{x}-{len(state['messages'])}-{TAG}"


@tool
def boom() -> str:
    """Always fails."""
    side("boom")
    raise ValueError("boom")


@record.tool(mutation_class="none")
def helper(x: str) -> dict:
    side(f"helper {x}")
    return {"x": x, "tag": TAG}


@tool
def wrapper(x: str) -> dict:
    """Calls a record.tool helper (a nested boundary)."""
    side(f"wrapper {x}")
    return {"inner": helper(x)}


class Echo(BaseTool):
    name: str = "echo"
    description: str = "Echo the query."

    def _run(self, q: str, run_manager=None) -> str:
        side(f"echo {q}")
        return f"echo-{q}-{TAG}"


TOOLS = wrap_tools(
    [lookup, ping, counter, stateful, boom, wrapper, Echo()],
    json.loads(os.environ.get("AGENT_CLASSES", "{}")),
)


def _plan_model(state):
    if any(isinstance(m, ToolMessage) for m in state["messages"]):
        return {"messages": [AIMessage("done")]}
    plan = json.loads(os.environ["AGENT_PLAN"])
    return {"messages": [AIMessage("", tool_calls=[
        {"name": op["tool"], "args": op.get("args", {}), "id": op.get("id") or f"call-{i}"}
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
        state = {"messages": [HumanMessage("go")]}
        if MODE == "ainvoke":
            result = asyncio.run(APP.ainvoke(state))
        else:
            result = APP.invoke(state)
        for m in result["messages"]:
            if isinstance(m, ToolMessage):
                out.append({"name": m.name, "id": m.tool_call_id, "response": m.content,
                            "status": m.status})
        out.append({"final": result["messages"][-1].content})
    except BaseException as exc:  # the workload swallows every failure
        out.append({"error": type(exc).__name__})
    with open(OUT, "w") as fh:
        json.dump(out, fh)


main()
'''


class Env:
    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.root = root
        self.agent = root / "agent.py"
        self.agent.write_text(AGENT)
        self.side = root / "side.log"
        self.out = root / "out.json"
        self.runs = root / "runs"
        self.replays = root / "replays"
        self._mp = monkeypatch
        monkeypatch.setenv("AGENT_SIDE", str(self.side))
        monkeypatch.setenv("AGENT_OUT", str(self.out))
        monkeypatch.setenv("NOVA_CAPTURE_LEVEL", "forensic")
        # LangSmith tracing would be live network; it is off unless configured.
        monkeypatch.setenv("LANGSMITH_TRACING", "false")
        for var in (
            "FORCE_COLOR", "COLORTERM", "AGENT_TAG", "AGENT_CLASSES", "AGENT_MODE",
            "AGENT_MODEL", "AGENT_CANNED", "NOVAFABRIC_TOOL_RESULT_MAX_BYTES",
            "LANGCHAIN_TRACING_V2", "LANGSMITH_API_KEY", "LANGCHAIN_API_KEY",
        ):
            monkeypatch.delenv(var, raising=False)

    def capture(self, plan: list[dict[str, Any]]) -> tuple[Path, list[dict[str, Any]]]:
        self._mp.setenv("AGENT_PLAN", json.dumps(plan))
        self._mp.setenv("AGENT_TAG", "captured")
        result = runner.invoke(
            app, ["capture", "--output-dir", str(self.runs), sys.executable, str(self.agent)]
        )
        assert result.exit_code == 0, result.output
        (capsule,) = [p for p in self.runs.iterdir() if (p / "capsule.yaml").exists()]
        captured = json.loads(self.out.read_text())
        self.out.unlink()
        if self.side.exists():
            self.side.unlink()
        self._mp.setenv("AGENT_TAG", "live-in-replay")
        self._mp.delenv("AGENT_CANNED", raising=False)
        return capsule, captured

    def records(self, capsule: Path) -> list[dict[str, Any]]:
        lines = (capsule / "tool-calls.jsonl").read_text().splitlines()
        return [json.loads(line) for line in lines if line.strip()]

    def write_records(self, capsule: Path, records: list[dict[str, Any]]) -> None:
        (capsule / "tool-calls.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in records)
        )

    def replay(
        self, capsule: Path, *flags: str, plan: list[dict[str, Any]] | None = None
    ) -> tuple[dict[str, Any], list[dict[str, Any]], Path]:
        if plan is not None:
            self._mp.setenv("AGENT_PLAN", json.dumps(plan))
        before = set(self.replays.iterdir()) if self.replays.exists() else set()
        runner.invoke(app, ["replay", str(capsule), "-o", str(self.replays), *flags])
        (replay_dir,) = set(self.replays.iterdir()) - before
        result = yaml.safe_load((replay_dir / "replay_result.yaml").read_text())
        jsonschema.validate(result, _RESULT_SCHEMA)
        observed = json.loads(self.out.read_text()) if self.out.exists() else []
        if self.out.exists():
            self.out.unlink()
        return result, observed, replay_dir

    @property
    def side_effects(self) -> list[str]:
        return self.side.read_text().splitlines() if self.side.exists() else []


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Env:
    return Env(tmp_path, monkeypatch)


def _kinds(result: dict[str, Any]) -> list[str]:
    return [d["kind"] for d in result["replay_contract"]["divergences"]]


def _call(tool: str, call_id: str | None = None, **args: Any) -> dict[str, Any]:
    return {"tool": tool, "args": args, "id": call_id}


def _responses(observed: list[dict[str, Any]]) -> list[tuple[str, Any]]:
    return sorted(
        ((o["name"], o["response"]) for o in observed if "name" in o), key=lambda t: str(t)
    )


def _refused(observed: list[dict[str, Any]]) -> bool:
    return any(o.get("error") for o in observed)


# ── served round trip ────────────────────────────────────────────────────────


@pytest.mark.parametrize("mode", ["invoke", "ainvoke"])
def test_langchain_tool_calls_are_served_and_their_bodies_never_run(
    env: Env, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    monkeypatch.setenv("AGENT_MODE", mode)
    plan = [_call("lookup", order_id=SENTINEL), _call("stateful", x=3), _call("echo", q="hi")]
    if mode == "ainvoke":  # an async-only tool cannot run in a sync graph
        plan.append(_call("ping"))
    capsule, captured = env.capture(plan)
    records = env.records(capsule)
    assert sorted(r["tool_name"] for r in records) == sorted(op["tool"] for op in plan)
    for rec in records:
        jsonschema.validate(rec, _TOOL_SCHEMA)
        assert rec["transport"] == "python"
        assert rec["extensions"]["io.novafabric.tool_surface"] == "langchain.tool"
        assert rec["extensions"]["io.novafabric.result_codec"] == "json-v1", rec["extensions"].get("io.novafabric.not_servable_reason")
        assert rec["extensions"]["io.novafabric.langchain_tool_call_id"].startswith("call-")
    lookup = next(r for r in records if r["tool_name"] == "lookup")
    assert lookup["arguments"] == {"order_id": SENTINEL}
    assert lookup["extensions"]["io.novafabric.tool_qualname"].endswith(":lookup")
    # Injected state and the call id are not arguments the model sent.
    stateful = next(r for r in records if r["tool_name"] == "stateful")
    assert stateful["arguments"] == {"x": 3}
    assert stateful["extensions"]["io.novafabric.ignored_arguments"] == [
        "state", "tool_call_id",
    ]

    result, replayed, replay_dir = env.replay(capsule)
    assert replayed == captured  # recorded values, not "live-in-replay"
    assert any("captured" in str(resp) for _, resp in _responses(captured))
    assert env.side_effects == [], "a LangChain tool body ran during mocked replay"
    assert result["status"] == "success", result
    assert result["tool_calls_mocked"] == result["tool_calls_available"] == len(plan)
    assert result["queues_fully_consumed"] is True
    contract = result["replay_contract"]
    assert contract["tool_calls_by_surface"][LC]["mocked"] == len(plan)
    assert LC in contract["interception_surfaces"]
    # No argument or result value reaches the replay's own outputs; the replay
    # capsule's call streams may only hold copies of the capsule's own records
    # (ADR-0321) -- the same rule as the ADK and Agents SDK e2e tests.
    assert_replay_outputs_hold_no_value(replay_dir, capsule, SENTINEL)


def test_a_langgraph_agent_with_a_served_openai_model_replays_faithfully(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Model and tools both served: the OpenAI SDK (fake transport) plus the seam."""
    pytest.importorskip("openai")
    pytest.importorskip("httpx")
    from _mocked_replay_agent import openai_body

    monkeypatch.setenv("AGENT_MODEL", "openai")
    monkeypatch.setenv("AGENT_CANNED", json.dumps([
        openai_body(tool_calls=[
            ("call_L", "lookup", {"order_id": SENTINEL}), ("call_E", "echo", {"q": "hi"}),
        ], rid="r1"),
        openai_body(content="all done", rid="r2"),
    ]))
    capsule, captured = env.capture([])
    assert captured[-1] == {"final": "all done"}
    assert {r["tool_name"] for r in env.records(capsule)} == {"lookup", "echo"}

    result, replayed, _ = env.replay(capsule)  # AGENT_CANNED unset: no network
    assert replayed == captured
    assert env.side_effects == []
    assert result["status"] == "success", result
    assert result["tool_calls_mocked"] == 2
    assert result["model_calls_mocked"] == 2
    assert result["queues_fully_consumed"] is True


# ── fail closed ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "plan",
    [
        [_call("lookup", order_id="o-1"), _call("echo", q="x")],  # no record
        [_call("lookup", order_id="o-2")],  # different arguments
    ],
    ids=["no-record", "different-arguments"],
)
def test_an_unmatched_langchain_call_fails_closed_before_the_tool_runs(
    env: Env, plan: list[dict[str, Any]]
) -> None:
    capsule, _ = env.capture([_call("lookup", order_id="o-1")])
    result, replayed, _ = env.replay(capsule, plan=plan)
    assert env.side_effects == [], "the live tool ran for an unmatched call"
    assert "tool_call_unmatched" in _kinds(result)
    assert result["status"] == "failure"
    assert result["error"]["type"] == "ReplayDivergence"
    # ToolNode re-raises the refusal (default handle_tool_errors), and the graph fails.
    assert {"error": "ReplayToolUnmatchedError"} in replayed, replayed
    assert result["replay_contract"]["tool_calls_by_surface"][LC]["unmatched"] >= 1


def test_a_capsule_without_langchain_records_fails_closed(env: Env) -> None:
    """Red check for serving: the same capsule with its tool records withheld."""
    capsule, _ = env.capture([_call("lookup", order_id="o-1")])
    env.write_records(capsule, [])
    result, _, _ = env.replay(capsule)
    assert env.side_effects == []
    assert result["status"] == "failure"
    assert "tool_call_unmatched" in _kinds(result)


# ── matching: the model's tool_call id feeds the id tier ─────────────────────


def test_the_tool_call_id_pairs_identical_calls_by_id(env: Env) -> None:
    capsule, captured = env.capture(
        [_call("counter", "call-A", key="k"), _call("counter", "call-B", key="k")]
    )
    by_id = {o["id"]: json.loads(o["response"])["n"] for o in captured if "id" in o}
    assert sorted(by_id.values()) == [1, 2]
    # Same name, same arguments, reversed order: only the id tells them apart.
    result, replayed, _ = env.replay(
        capsule, plan=[_call("counter", "call-B", key="k"), _call("counter", "call-A", key="k")]
    )
    assert env.side_effects == []
    assert result["status"] == "success", result
    assert {o["id"]: json.loads(o["response"])["n"] for o in replayed if "id" in o} == by_id


# ── permissive keeps the safety ladder; overrides hold ───────────────────────


def test_permissive_runs_an_unmatched_tool_live_only_with_the_ladder(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENT_CLASSES", json.dumps({"echo": "none"}))
    capsule, _ = env.capture([_call("lookup", order_id="o-1")])
    plan = [_call("lookup", order_id="o-1"), _call("echo", q="x")]
    result, replayed, _ = env.replay(capsule, "--permissive", plan=plan)
    assert env.side_effects == ["echo x"], "a `none` tool runs live under --permissive"
    assert result["tool_calls_live"] == 1
    assert ("echo", "echo-x-live-in-replay") in _responses(replayed)

    # `lookup` is `unknown` (the default): refused without --allow-unknown-mutation.
    env.side.unlink()
    plan = [_call("lookup", order_id="o-1"), _call("lookup", order_id="o-9")]
    result, replayed, _ = env.replay(capsule, "--permissive", plan=plan)
    assert env.side_effects == []
    assert result["replay_contract"]["tool_calls_refused"] == 1
    assert _refused(replayed)


def test_a_replay_yaml_allow_false_override_holds_under_permissive(env: Env) -> None:
    capsule, _ = env.capture([_call("lookup", order_id="o-1")])
    (capsule / "replay.yaml").write_text(yaml.safe_dump({
        "schema_version": "0.1.0",
        "tool_overrides": [{"tool_name": "lookup", "allow": False}],
    }))
    plan = [_call("lookup", order_id="o-1"), _call("lookup", order_id="o-2")]
    result, replayed, _ = env.replay(
        capsule, "--permissive", "--allow-unknown-mutation", plan=plan
    )
    assert env.side_effects == [], "`allow: false` must never run the tool live"
    assert result["replay_contract"]["tool_calls_refused"] == 1
    assert result["tool_calls_mocked"] == 1  # the matched call is still served
    assert _refused(replayed)
    (entry,) = result["replay_contract"]["tool_overrides"]
    assert entry["tool_name"] == "lookup"


# ── not servable ─────────────────────────────────────────────────────────────


def test_payloads_off_records_digests_only_and_replay_asks_for_a_recapture(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOVA_CAPTURE_LEVEL", "standard")
    capsule, _ = env.capture([_call("lookup", order_id=SENTINEL)])
    (rec,) = env.records(capsule)
    jsonschema.validate(rec, _TOOL_SCHEMA)
    assert SENTINEL not in json.dumps(rec)
    assert rec["arguments"] == {} and rec["result"] is None
    assert rec["extensions"]["io.novafabric.arguments_digest"]
    assert rec["extensions"]["io.novafabric.result_digest"].startswith("sha256:")
    assert rec["extensions"]["io.novafabric.result_codec"] == "not-servable"
    result, _, _ = env.replay(capsule)
    assert env.side_effects == []
    assert result["status"] == "failure"
    (div,) = [
        d for d in result["replay_contract"]["divergences"]
        if d["kind"] == "tool_result_not_servable"
    ]
    assert "NOVA_CAPTURE_LEVEL=forensic" in div["reason"]
    assert div["surface"] == LC


def test_a_nested_record_makes_the_langchain_call_unservable(env: Env) -> None:
    capsule, _ = env.capture([_call("wrapper", x="a")])
    records = env.records(capsule)
    inner = next(r for r in records if r["tool_name"] == "helper")
    outer = next(r for r in records if r["tool_name"] == "wrapper")
    assert inner["extensions"]["io.novafabric.within_tool_call_id"] == outer["tool_call_id"]
    assert outer["extensions"]["io.novafabric.nested_records"] == 1
    assert outer["extensions"]["io.novafabric.result_codec"] == "not-servable"
    result, _, _ = env.replay(capsule)
    assert env.side_effects == [], "neither the LangChain tool nor its nested tool may run"
    assert result["status"] == "failure"
    (div,) = [
        d for d in result["replay_contract"]["divergences"]
        if d["kind"] == "tool_result_not_servable"
    ]
    assert "inside this LangChain tool call" in div["reason"]


def test_a_failed_tool_call_is_recorded_but_never_served(env: Env) -> None:
    capsule, captured = env.capture([_call("boom")])
    assert captured == [{"error": "ValueError"}]  # ToolNode re-raises by default
    (rec,) = env.records(capsule)
    jsonschema.validate(rec, _TOOL_SCHEMA)
    assert rec["status"] == "error"
    assert rec["extensions"]["io.novafabric.result_codec"] == "not-servable"
    result, _, _ = env.replay(capsule)
    assert env.side_effects == []
    assert result["status"] == "failure"
    assert "tool_result_not_servable" in _kinds(result)
