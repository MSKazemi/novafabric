"""OpenAI Agents SDK function tools NOT passed through ``wrap_tools``: capture
records them, mocked replay serves a recorded result by default, and never
runs one without counting it (ADR-0306 r3 amendment, owner decision 2026-11-21).

Before this, an unwrapped ``@function_tool`` was invisible to both capture and
replay: its body ran live in a "mocked" replay and the report said
``tool_calls_live: 0``. The seam's *observer* patches the SDK's private
function-tool invocation helper (``agents.tool._invoke_function_tool_with_metadata``,
checked against ``openai-agents`` 0.24.0 only -- not an SDK extension point) and
hands every unwrapped call to the same handler as ``wrap_tools`` copies, with
mutation class ``unknown``. Agents used as tools (``Agent.as_tool``) are observed
too (ADR-0306 r3c): served when recorded, their sub-run's calls covered.

The workload is a real ``agents`` ``Runner`` with an offline fake ``Model`` (no
network, no API key).
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

AGENT = '''
import asyncio, json, os

from agents import Agent, ModelResponse, Runner, Usage, function_tool
from agents.models.interface import Model
from openai.types.responses import (
    ResponseFunctionToolCall, ResponseOutputMessage, ResponseOutputText,
)

SIDE = os.environ["AGENT_SIDE"]
OUT = os.environ["AGENT_OUT"]


def side(text):
    with open(SIDE, "a") as fh:
        fh.write(text + "\\n")


@function_tool
def lookup(order_id: str) -> dict:
    side(f"lookup {order_id}")
    return {"status": "shipped", "id": order_id}


@function_tool
def write_note(text: str) -> str:
    side(f"write_note {text}")
    return f"saved {text}"


@function_tool
def inner(x: str) -> str:
    side(f"inner {x}")
    return f"inner-{x}"


class FakeModel(Model):
    """Offline: asks for the planned tool calls once, then answers with *final*."""

    def __init__(self, plan, final):
        self.plan, self.final, self.turn = plan, final, 0

    def _output(self):
        self.turn += 1
        if self.turn == 1 and self.plan:
            return [
                ResponseFunctionToolCall(
                    type="function_call", id=f"fc{i}", call_id=op.get("id") or f"call-{i}",
                    name=op["tool"], arguments=json.dumps(op.get("args", {})),
                )
                for i, op in enumerate(self.plan)
            ]
        return [ResponseOutputMessage(
            id="m", type="message", role="assistant", status="completed",
            content=[ResponseOutputText(type="output_text", text=self.final, annotations=[])],
        )]

    async def get_response(self, *args, **kwargs):
        return ModelResponse(output=self._output(), usage=Usage(), response_id=None)

    async def stream_response(self, *args, **kwargs):
        raise NotImplementedError
        yield


PLAN = json.loads(os.environ["AGENT_PLAN"])
WRAP = set(json.loads(os.environ.get("AGENT_WRAP", "[]")))


async def main():
    helper = Agent(
        name="helper",
        model=FakeModel([{"tool": "inner", "args": {"x": "h"}, "id": "call-inner"}], "helped"),
        tools=[inner],
    )
    tools = [lookup, write_note, helper.as_tool("ask_helper", "Ask the helper agent.")]
    if WRAP:
        from novafabric.adapters.openai_agents import wrap_tools

        tools = [wrap_tools([t])[0] if t.name in WRAP else t for t in tools]
    agent = Agent(name="agent", model=FakeModel(PLAN, "done"), tools=tools)
    out = []
    try:
        result = await Runner.run(agent, "go")
        names = {}
        for item in result.new_items:
            if item.type == "tool_call_item":
                names[item.raw_item.call_id] = item.raw_item.name
            elif item.type == "tool_call_output_item":
                call_id = item.raw_item["call_id"]
                out.append({"name": names.get(call_id), "response": item.output})
        out.append({"final": result.final_output})
    except BaseException as exc:  # the workload swallows every failure
        out.append({"error": type(exc).__name__})
    with open(OUT, "w") as fh:
        json.dump(out, fh)


asyncio.run(main())
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
        monkeypatch.setenv("OPENAI_AGENTS_DISABLE_TRACING", "1")
        for var in ("FORCE_COLOR", "COLORTERM", "AGENT_WRAP", "NOVA_CAPTURE_LEVEL"):
            monkeypatch.delenv(var, raising=False)

    def capture(
        self, plan: list[dict[str, Any]], *, level: str | None = None
    ) -> tuple[Path, list[dict[str, Any]]]:
        self._mp.setenv("AGENT_PLAN", json.dumps(plan))
        if level is not None:
            self._mp.setenv("NOVA_CAPTURE_LEVEL", level)
        result = runner.invoke(
            app, ["capture", "--output-dir", str(self.runs), sys.executable, str(self.agent)]
        )
        self._mp.delenv("NOVA_CAPTURE_LEVEL", raising=False)
        assert result.exit_code == 0, result.output
        (capsule,) = [p for p in self.runs.iterdir() if (p / "capsule.yaml").exists()]
        captured = json.loads(self.out.read_text())
        self.out.unlink()
        if self.side.exists():
            self.side.unlink()
        return capsule, captured

    def records(self, capsule: Path) -> list[dict[str, Any]]:
        path = capsule / "tool-calls.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

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

    def report(self, result: dict[str, Any]) -> str:
        return (self.replays / result["replay_id"] / "dry_run_report.txt").read_text()

    @property
    def side_effects(self) -> list[str]:
        return self.side.read_text().splitlines() if self.side.exists() else []


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Env:
    return Env(tmp_path, monkeypatch)


PLAN = [
    {"tool": "lookup", "args": {"order_id": "o-1"}},
    {"tool": "write_note", "args": {"text": "hello"}},
]


def _kinds(result: dict[str, Any]) -> list[str]:
    return [d["kind"] for d in result["replay_contract"]["divergences"]]


def test_capture_records_unwrapped_function_tools_as_observed(env: Env) -> None:
    capsule, captured = env.capture(PLAN, level="forensic")
    assert {"final": "done"} in captured
    records = env.records(capsule)
    assert sorted(r["tool_name"] for r in records) == ["lookup", "write_note"]
    for rec in records:
        jsonschema.validate(rec, _TOOL_SCHEMA)
        ext = rec["extensions"]
        assert ext["io.novafabric.tool_surface"] == "openai.agents.tool"
        assert ext["io.novafabric.openai_agents_tool_seam"] == "observed"
        assert rec["mutation_class"] == "unknown"
        assert ext["io.novafabric.result_codec"] == "json-v1"
        assert ext["io.novafabric.openai_agents_call_id"].startswith("call-")
    lookup = next(r for r in records if r["tool_name"] == "lookup")
    assert lookup["arguments"] == {"order_id": "o-1"}
    assert lookup["result"]["value"] == {"status": "shipped", "id": "o-1"}

    # Default level: no payload, so not servable, and the reason says how to fix it.
    env.runs = env.runs.parent / "runs-default"
    capsule, _ = env.capture(PLAN)
    for rec in env.records(capsule):
        assert rec["extensions"]["io.novafabric.openai_agents_tool_seam"] == "observed"
        assert rec["extensions"]["io.novafabric.result_codec"] == "not-servable"
        assert "NOVA_CAPTURE_LEVEL=forensic" in rec["extensions"][
            "io.novafabric.not_servable_reason"
        ]


def test_an_agent_used_as_a_tool_is_a_served_boundary_its_inner_tool_is_covered(
    env: Env,
) -> None:
    """ADR-0306 r3c: an ``Agent.as_tool`` call is observed like any function tool;
    replay serves it and consumes its sub-run's tool call as covered."""
    capsule, captured = env.capture(
        [{"tool": "ask_helper", "args": {"input": "please"}}], level="forensic"
    )
    assert {"name": "ask_helper", "response": "helped"} in captured
    records = {r["tool_name"]: r for r in env.records(capsule)}
    assert set(records) == {"inner", "ask_helper"}
    outer = records["ask_helper"]
    assert records["inner"]["extensions"]["io.novafabric.within_tool_call_id"] == (
        outer["tool_call_id"]
    )
    assert outer["extensions"]["io.novafabric.openai_agents_tool_seam"] == "observed"
    assert outer["extensions"]["io.novafabric.result_codec"] == "json-v1"

    result, observed, cli = env.replay(capsule)
    assert cli.exit_code == 0, cli.output
    assert observed == captured
    assert env.side_effects == [], "the agent-as-tool's inner tool ran during mocked replay"
    assert result["tool_calls_mocked"] == 1
    assert result["replay_contract"]["tool_calls_covered"] == 1


def test_strict_replay_serves_unwrapped_function_tools_and_never_runs_their_bodies(
    env: Env,
) -> None:
    capsule, captured = env.capture(PLAN, level="forensic")
    result, observed, cli = env.replay(capsule)
    assert cli.exit_code == 0, cli.output
    assert result["status"] == "success", result.get("error")
    assert env.side_effects == [], "an unwrapped function tool ran during mocked replay"
    assert observed == captured
    assert result["tool_calls_mocked"] == 2
    assert result["tool_calls_live"] == 0
    contract = result["replay_contract"]
    assert contract["tool_calls_by_surface"][OAI]["mocked"] == 2
    assert contract["divergences"] == []
    assert result["queues_fully_consumed"] is True

    dry, _, cli = env.replay(capsule, "--dry-run")
    assert cli.exit_code == 0, cli.output
    assert "[MOCK] lookup" in env.report(dry)


def test_strict_replay_refuses_to_start_when_unwrapped_results_were_not_recorded(
    env: Env,
) -> None:
    capsule, _ = env.capture(PLAN)  # default level: no payloads
    result, observed, cli = env.replay(capsule)
    assert observed is None and env.side_effects == [], "the workload must not run"
    assert cli.exit_code == 1, cli.output
    assert result["status"] == "aborted"
    assert result["error"]["type"] == "ToolSurfaceNotServed"
    by_surface = result["error"]["tool_calls_not_served_by_surface"]
    assert by_surface[OAI]["count"] == 2
    assert by_surface[OAI]["tools"] == ["lookup", "write_note"]
    assert "NOVA_CAPTURE_LEVEL=forensic" in result["error"]["message"]

    dry, _, cli = env.replay(capsule, "--dry-run")
    assert cli.exit_code == 1
    report = env.report(dry)
    assert report.startswith("REFUSED:")
    assert "[NOT SERVED] OpenAI Agents tool call (lookup)" in report


def test_a_capsule_without_tool_records_refuses_strict_and_counts_permissive(
    env: Env,
) -> None:
    """The v0.108.0 shape: the capsule holds no Agents tool record (the observer
    did not exist), so nothing is known up front. Strict refuses the call before
    its body runs; ``--permissive --allow-unknown-mutation`` runs it live, counted.
    Before: the body ran live and the report said ``tool_calls_live: 0``."""
    capsule, captured = env.capture(PLAN, level="forensic")
    (capsule / "tool-calls.jsonl").write_text("")

    result, observed, cli = env.replay(capsule)
    assert env.side_effects == [], "a strict replay ran an unrecorded function tool"
    assert cli.exit_code == 1, cli.output
    assert result["status"] == "failure"
    assert "tool_call_unmatched" in _kinds(result)
    assert result["tool_calls_live"] == 0

    result, observed, cli = env.replay(capsule, "--permissive")
    assert env.side_effects == [], "--permissive alone ran an unwrapped tool live"

    result, observed, cli = env.replay(capsule, "--permissive", "--allow-unknown-mutation")
    assert observed == captured
    assert sorted(env.side_effects) == ["lookup o-1", "write_note hello"]
    assert result["tool_calls_live"] == 2
    assert result["fidelity"]["verdict"] != "faithful"
    assert "[LIVE] tool call (agents.FunctionTool: lookup)" in cli.output


def test_wrapped_and_unwrapped_function_tools_one_record_per_call(env: Env) -> None:
    env._mp.setenv("AGENT_WRAP", json.dumps(["lookup"]))
    capsule, captured = env.capture(PLAN, level="forensic")
    seams = {
        r["tool_name"]: r["extensions"].get("io.novafabric.openai_agents_tool_seam")
        for r in env.records(capsule)
    }
    assert seams == {"lookup": "wrap_tools", "write_note": "observed"}

    result, observed, _ = env.replay(capsule)
    assert observed == captured
    assert env.side_effects == []
    assert result["tool_calls_mocked"] == 2


def test_the_observer_patch_is_restored_on_uninstall() -> None:
    import agents.run_internal.tool_execution as tool_execution
    import agents.tool as sdk_tool

    from novafabric.adapters import _oai_tool_seam as seam

    before = (
        sdk_tool._invoke_function_tool_with_metadata,
        tool_execution._invoke_function_tool_with_metadata,
    )
    seam.install_observer()
    try:
        assert sdk_tool._invoke_function_tool_with_metadata is not before[0]
        assert tool_execution._invoke_function_tool_with_metadata is not before[1]
    finally:
        seam.uninstall_observer()
    assert (
        sdk_tool._invoke_function_tool_with_metadata,
        tool_execution._invoke_function_tool_with_metadata,
    ) == before
