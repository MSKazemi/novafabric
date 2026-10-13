"""ADR-0306 slice 3 (OpenAI Agents SDK): function-tool calls round-trip through real
``nova capture`` and mocked ``nova replay`` subprocesses.

The workload is a real ``agents`` ``Runner`` with an offline fake ``Model`` (no
network, no API key) and its tools routed through ``wrap_tools``. The seam
replaces ``FunctionTool.on_invoke_tool`` -- an undocumented SDK seam -- so these
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
from typer.testing import CliRunner

from novafabric.cli.main import app

pytest.importorskip("agents")

runner = CliRunner()

REPO = Path(__file__).resolve().parents[2]
_RESULT_SCHEMA = json.loads(
    (REPO / "src" / "novafabric" / "schemas" / "replay-result.schema.json").read_text()
)
_TOOL_SCHEMA = json.loads(
    (REPO / "src" / "novafabric" / "schemas" / "tool-call.schema.json").read_text()
)

OAI = "agents.FunctionTool"

#: Never appears in source the replay reads; checked for in the event log/result.
SENTINEL = "SENTINEL-oai-7c2a"

AGENT = '''
import asyncio, json, os

from agents import Agent, ModelResponse, Runner, Usage, function_tool
from agents.models.interface import Model
from openai.types.responses import (
    Response, ResponseCompletedEvent, ResponseFunctionToolCall,
    ResponseOutputMessage, ResponseOutputText,
)

from novafabric.adapters.openai_agents import wrap_tools
from novafabric.capture import record

SIDE = os.environ["AGENT_SIDE"]
OUT = os.environ["AGENT_OUT"]
TAG = os.environ.get("AGENT_TAG", "")
STREAM = os.environ.get("AGENT_STREAM") == "1"
COUNT = {"n": 0}


def side(text):
    with open(SIDE, "a") as fh:
        fh.write(text + "\\n")


@function_tool
def lookup(order_id: str) -> dict:
    side(f"lookup {order_id}")
    return {"status": "shipped", "id": order_id, "tag": TAG}


@function_tool
def ping() -> str:
    side("ping")
    return "pong-" + TAG


@function_tool
def nothing() -> None:
    side("nothing")
    return None


@function_tool
def counter(key: str) -> dict:
    side(f"counter {key}")
    COUNT["n"] += 1
    return {"key": key, "n": COUNT["n"], "tag": TAG}


@function_tool
def boom() -> str:
    side("boom")
    raise ValueError("boom")


@record.tool(mutation_class="none")
def helper(x: str) -> dict:
    side(f"helper {x}")
    return {"x": x, "tag": TAG}


@function_tool
def wrapper(x: str) -> dict:
    side(f"wrapper {x}")
    return {"inner": helper(x)}


PLAN = json.loads(os.environ["AGENT_PLAN"])


def _response(output):
    return Response(
        id="r", created_at=0, model="fake", object="response", output=output,
        parallel_tool_calls=True, tool_choice="auto", tools=[],
    )


class FakeModel(Model):
    """Offline: asks for the planned tool calls once, then finishes."""

    def __init__(self):
        self.turn = 0

    def _output(self):
        self.turn += 1
        if self.turn == 1:
            return [
                ResponseFunctionToolCall(
                    type="function_call", id=f"fc{i}",
                    call_id=op.get("id") or f"call-{i}", name=op["tool"],
                    arguments=json.dumps(op.get("args", {})),
                )
                for i, op in enumerate(PLAN)
            ]
        return [ResponseOutputMessage(
            id="m", type="message", role="assistant", status="completed",
            content=[ResponseOutputText(type="output_text", text="done", annotations=[])],
        )]

    async def get_response(self, *args, **kwargs):
        return ModelResponse(output=self._output(), usage=Usage(), response_id=None)

    async def stream_response(self, *args, **kwargs):
        yield ResponseCompletedEvent(
            type="response.completed", sequence_number=0, response=_response(self._output())
        )


async def main():
    tools = wrap_tools(
        [lookup, ping, nothing, counter, boom, wrapper],
        json.loads(os.environ.get("AGENT_CLASSES", "{}")),
    )
    agent = Agent(name="agent", model=FakeModel(), tools=tools)
    out = []
    try:
        if STREAM:
            result = Runner.run_streamed(agent, "go")
            async for _ in result.stream_events():
                pass
        else:
            result = await Runner.run(agent, "go")
        names = {}
        for item in result.new_items:
            if item.type == "tool_call_item":
                names[item.raw_item.call_id] = item.raw_item.name
            elif item.type == "tool_call_output_item":
                call_id = item.raw_item["call_id"]
                out.append({"name": names.get(call_id), "id": call_id, "response": item.output})
    except BaseException as exc:  # the workload swallows every failure
        cause = exc.__cause__
        out.append({
            "error": type(exc).__name__,
            "cause": type(cause).__name__ if cause is not None else None,
        })
    with open(OUT, "w") as fh:
        json.dump(out, fh)


asyncio.run(main())
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
        monkeypatch.setenv("OPENAI_AGENTS_DISABLE_TRACING", "1")
        for var in (
            "FORCE_COLOR", "COLORTERM", "AGENT_TAG", "AGENT_CLASSES", "AGENT_STREAM",
            "NOVAFABRIC_TOOL_RESULT_MAX_BYTES",
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
    return any(o.get("error") for o in observed) or any(
        "ReplayToolUnmatchedError" in json.dumps(o) for o in observed
    )


# ── served round trip ────────────────────────────────────────────────────────


@pytest.mark.parametrize("stream", [False, True], ids=["run", "run_streamed"])
def test_agents_tool_calls_are_served_and_their_bodies_never_run(
    env: Env, monkeypatch: pytest.MonkeyPatch, stream: bool
) -> None:
    if stream:
        monkeypatch.setenv("AGENT_STREAM", "1")
    plan = [_call("lookup", order_id=SENTINEL), _call("ping"), _call("nothing")]
    capsule, captured = env.capture(plan)
    records = env.records(capsule)
    assert sorted(r["tool_name"] for r in records) == ["lookup", "nothing", "ping"]
    for rec in records:
        jsonschema.validate(rec, _TOOL_SCHEMA)
        assert rec["transport"] == "python"
        assert rec["extensions"]["io.novafabric.tool_surface"] == "openai.agents.tool"
        assert rec["extensions"]["io.novafabric.result_codec"] == "json-v1", rec
        assert rec["extensions"]["io.novafabric.openai_agents_call_id"].startswith("call-")
    lookup = next(r for r in records if r["tool_name"] == "lookup")
    assert lookup["arguments"] == {"order_id": SENTINEL}
    # The user's function, not the SDK's wrapper class (read before the seam replaces
    # the invoker, after which the SDK's `__wrapped__` raises).
    assert lookup["extensions"]["io.novafabric.tool_qualname"].endswith(":lookup")

    result, replayed, replay_dir = env.replay(capsule)
    assert _responses(replayed) == _responses(captured)  # recorded, not "live-in-replay"
    assert any(
        "captured" in json.dumps(resp) for name, resp in _responses(captured) if name == "lookup"
    )
    assert env.side_effects == [], "an Agents SDK tool body ran during mocked replay"
    assert result["status"] == "success", result
    assert result["tool_calls_mocked"] == result["tool_calls_available"] == 3
    assert result["queues_fully_consumed"] is True
    contract = result["replay_contract"]
    assert contract["tool_calls_by_surface"][OAI]["mocked"] == 3
    assert OAI in contract["interception_surfaces"]
    # No argument or result value reaches the replay's own outputs.
    for path in replay_dir.rglob("*"):
        if path.is_file():
            assert SENTINEL not in path.read_text(errors="replace"), path


# ── fail closed ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "plan",
    [
        [_call("lookup", order_id="o-1"), _call("ping")],  # no record
        [_call("lookup", order_id="o-2")],  # different arguments
    ],
    ids=["no-record", "different-arguments"],
)
def test_an_unmatched_agents_call_fails_closed_before_the_tool_runs(
    env: Env, plan: list[dict[str, Any]]
) -> None:
    capsule, _ = env.capture([_call("lookup", order_id="o-1")])
    result, replayed, _ = env.replay(capsule, plan=plan)
    assert env.side_effects == [], "the live tool ran for an unmatched call"
    assert "tool_call_unmatched" in _kinds(result)
    assert result["status"] == "failure"
    assert result["error"]["type"] == "ReplayDivergence"
    # The Agents SDK re-raises the refusal as a UserError, with ours as its cause.
    assert {"error": "UserError", "cause": "ReplayToolUnmatchedError"} in replayed, replayed
    assert result["replay_contract"]["tool_calls_by_surface"][OAI]["unmatched"] >= 1


def test_a_capsule_without_agents_records_fails_closed(env: Env) -> None:
    """Red check for serving: the same capsule with its tool records withheld."""
    capsule, _ = env.capture([_call("lookup", order_id="o-1")])
    env.write_records(capsule, [])
    result, _, _ = env.replay(capsule)
    assert env.side_effects == []
    assert result["status"] == "failure"
    assert "tool_call_unmatched" in _kinds(result)


# ── matching: the model's call_id feeds the id tier ──────────────────────────


def test_the_call_id_pairs_identical_calls_by_id(env: Env) -> None:
    capsule, captured = env.capture(
        [_call("counter", "call-A", key="k"), _call("counter", "call-B", key="k")]
    )
    by_id = {o["id"]: json.loads(o["response"])["n"] if isinstance(o["response"], str)
             else o["response"]["n"] for o in captured}
    assert sorted(by_id.values()) == [1, 2]
    # Same name, same arguments, reversed order: only the id tells them apart.
    result, replayed, _ = env.replay(
        capsule, plan=[_call("counter", "call-B", key="k"), _call("counter", "call-A", key="k")]
    )
    assert env.side_effects == []
    assert result["status"] == "success", result
    assert {
        o["id"]: json.loads(o["response"])["n"] if isinstance(o["response"], str)
        else o["response"]["n"]
        for o in replayed
    } == by_id


# ── permissive keeps the safety ladder; overrides hold ───────────────────────


def test_permissive_runs_an_unmatched_tool_live_only_with_the_ladder(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENT_CLASSES", json.dumps({"ping": "none"}))
    capsule, _ = env.capture([_call("lookup", order_id="o-1")])
    plan = [_call("lookup", order_id="o-1"), _call("ping")]
    result, replayed, _ = env.replay(capsule, "--permissive", plan=plan)
    assert env.side_effects == ["ping"], "a `none` tool runs live under --permissive"
    assert result["tool_calls_live"] == 1
    assert ("ping", "pong-live-in-replay") in _responses(replayed)

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
    assert div["surface"] == OAI


def test_a_nested_record_makes_the_agents_call_unservable(env: Env) -> None:
    capsule, _ = env.capture([_call("wrapper", x="a")])
    records = env.records(capsule)
    inner = next(r for r in records if r["tool_name"] == "helper")
    outer = next(r for r in records if r["tool_name"] == "wrapper")
    assert inner["extensions"]["io.novafabric.within_tool_call_id"] == outer["tool_call_id"]
    assert outer["extensions"]["io.novafabric.nested_records"] == 1
    assert outer["extensions"]["io.novafabric.result_codec"] == "not-servable"
    result, _, _ = env.replay(capsule)
    assert env.side_effects == [], "neither the Agents SDK tool nor its nested tool may run"
    assert result["status"] == "failure"
    (div,) = [
        d for d in result["replay_contract"]["divergences"]
        if d["kind"] == "tool_result_not_servable"
    ]
    assert "inside this Agents SDK tool call" in div["reason"]


def test_a_failed_tool_call_is_recorded_but_never_served(env: Env) -> None:
    capsule, captured = env.capture([_call("boom")])
    (rec,) = env.records(capsule)
    jsonschema.validate(rec, _TOOL_SCHEMA)
    # The SDK's own failure handler turned the raise into a model-visible string.
    assert rec["status"] == "success"
    assert "error occurred" in json.dumps(captured)
