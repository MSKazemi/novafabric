"""An OpenAI Agents SDK ``Agent.as_tool`` (a sub-agent used as a tool) through real
``nova capture`` and mocked ``nova replay`` subprocesses (ADR-0306 r3c).

Before r3c the observer stepped aside for agents used as tools, and a wrapped
one was marked not servable because its body made calls. Now the LangChain model
(r3b) applies: the outer call is served from the capsule, the sub-agent's run
never happens, and its model and tool calls are consumed as *covered*. An
``Agent.as_tool`` whose sub-run has an effect beyond the returned value -- an
``on_stream`` / ``hooks`` / ``session`` option, a nested approval -- is recorded
as not servable instead (unit tests in ``tests/adapters``).

The workload is a real ``agents`` ``Runner`` with the SDK's own
``OpenAIResponsesModel`` over the real ``openai`` SDK and an
``httpx.MockTransport`` (no network, no API key), so every model call is
recorded and served. (``OpenAIChatCompletionsModel`` is covered by
``test_openai_agents_chat_completions_e2e.py``.)
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

OAI = "agents.FunctionTool"
WITHIN = "io.novafabric.within_tool_call_id"
NESTED = "io.novafabric.nested_records"

AGENT = '''
import asyncio, json, os

import httpx
from openai import AsyncOpenAI
from agents import Agent, OpenAIResponsesModel, Runner, function_tool

SIDE = os.environ["AGENT_SIDE"]
OUT = os.environ["AGENT_OUT"]
CANNED = json.loads(os.environ.get("AGENT_CANNED") or "null")
WRAP = os.environ.get("AGENT_WRAP") == "1"
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


CLIENT = AsyncOpenAI(api_key="sk-test", max_retries=0,
                     http_client=httpx.AsyncClient(transport=httpx.MockTransport(transport)))
MODEL = OpenAIResponsesModel(model="gpt-4o", openai_client=CLIENT)


@function_tool
def lookup(order_id: str) -> dict:
    """Look up an order (called by the sub-agent only)."""
    side(f"lookup {order_id}")
    return {"status": "shipped", "id": order_id}


async def main():
    helper = Agent(name="helper", model=MODEL, tools=[lookup])
    ask = helper.as_tool("ask_helper", "Ask the helper agent.")
    if WRAP:
        from novafabric.adapters.openai_agents import wrap_tools

        (ask,) = wrap_tools([ask])
    agent = Agent(name="agent", model=MODEL, tools=[ask])
    out = []
    try:
        result = await Runner.run(agent, "where is order 7?")
        names = {}
        for item in result.new_items:
            if item.type == "tool_call_item":
                names[item.raw_item.call_id] = item.raw_item.name
            elif item.type == "tool_call_output_item":
                out.append({"name": names.get(item.raw_item["call_id"]),
                            "response": item.output})
        out.append({"final": result.final_output})
    except BaseException as exc:  # the workload swallows every failure
        out.append({"error": type(exc).__name__, "message": str(exc)[:300]})
    with open(OUT, "w") as fh:
        json.dump(out, fh)


asyncio.run(main())
'''


def _canned() -> list[dict[str, Any]]:
    from _mocked_replay_agent import responses_body

    return [
        responses_body(calls=[("call_h", "ask_helper", {"input": "find order 7"})],
                       rid="outer-1"),
        responses_body(calls=[("call_l", "lookup", {"order_id": "7"})], rid="sub-1"),
        responses_body(text="order 7 has shipped", rid="sub-2"),
        responses_body(text="Order 7 has shipped.", rid="outer-2"),
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
        monkeypatch.setenv("OPENAI_AGENTS_DISABLE_TRACING", "1")
        for var in ("FORCE_COLOR", "COLORTERM", "AGENT_CANNED", "AGENT_WRAP",
                    "NOVA_CAPTURE_LEVEL"):
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

    def report(self, result: dict[str, Any]) -> str:
        return (self.replays / result["replay_id"] / "dry_run_report.txt").read_text()

    @property
    def side_effects(self) -> list[str]:
        return self.side.read_text().splitlines() if self.side.exists() else []


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Env:
    return Env(tmp_path, monkeypatch)


def _by_name(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {r["tool_name"]: r for r in records}


@pytest.mark.parametrize("wrap", ["0", "1"])
def test_capture_marks_the_sub_run_calls_inside_the_agent_tool(
    env: Env, monkeypatch: pytest.MonkeyPatch, wrap: str,
) -> None:
    monkeypatch.setenv("AGENT_WRAP", wrap)
    capsule, captured = env.capture()
    assert captured[-1] == {"final": "Order 7 has shipped."}
    assert captured[0] == {"name": "ask_helper", "response": "order 7 has shipped"}
    tools = _by_name(env.read(capsule / "tool-calls.jsonl"))
    assert set(tools) == {"ask_helper", "lookup"}
    for rec in tools.values():
        jsonschema.validate(rec, _TOOL_SCHEMA)
    outer, inner = tools["ask_helper"], tools["lookup"]
    assert outer["extensions"]["io.novafabric.openai_agents_tool_seam"] == (
        "wrap_tools" if wrap == "1" else "observed"
    )
    assert inner["extensions"][WITHIN] == outer["tool_call_id"]
    models = [
        r for r in env.read(capsule / "model-calls.jsonl")
        if (r.get("extensions") or {}).get("io.novafabric.record_role") == "logical"
    ]
    inside = [m for m in models if (m.get("extensions") or {}).get(WITHIN)]
    assert len(models) == 4 and len(inside) == 2
    assert {m["extensions"][WITHIN] for m in inside} == {outer["tool_call_id"]}
    assert outer["extensions"][NESTED] >= 3
    assert outer["extensions"]["io.novafabric.result_codec"] == "json-v1", outer
    assert outer["result"] == {"value": "order 7 has shipped"}


@pytest.mark.parametrize("wrap", ["0", "1"])
def test_strict_replay_serves_the_agent_tool_and_covers_the_sub_run(
    env: Env, monkeypatch: pytest.MonkeyPatch, wrap: str,
) -> None:
    monkeypatch.setenv("AGENT_WRAP", wrap)
    capsule, captured = env.capture()
    result, observed, cli = env.replay(capsule)  # strict, AGENT_CANNED unset
    assert cli.exit_code == 0, cli.output
    assert result["status"] == "success", result.get("error")
    assert env.side_effects == [], "the sub-agent's tool ran"
    assert observed == captured
    contract = result["replay_contract"]
    assert contract["divergences"] == []
    assert result["tool_calls_mocked"] == 1  # only the agent-tool call was asked for
    assert result["tool_calls_live"] == 0
    assert contract["tool_calls_covered"] == 1  # the sub-agent's lookup
    assert contract["tool_calls_by_surface"][OAI]["covered"] == 1
    assert contract["tool_calls_unconsumed"] == 0
    assert result["model_calls_mocked"] == 2  # the root agent's two calls
    assert contract["model_calls_covered"] == 2  # the sub-agent's two calls
    assert contract["model_calls_unconsumed"] == 0
    assert result["fidelity"]["verdict"] == "faithful", result["fidelity"]

    dry, _, cli = env.replay(capsule, "--dry-run")
    assert cli.exit_code == 0, cli.output
    report = env.report(dry)
    assert "[MOCK] ask_helper" in report
    assert "[COVERED by ask_helper] lookup" in report


def test_a_default_level_capsule_whose_outer_result_is_lost_covers_nothing(env: Env) -> None:
    """Below forensic only digests are kept, and the Responses API request input is
    not recorded at that level, so the echo cannot recover the outer result. The
    outer call is not served, so nothing it wrote is covered: a strict replay
    refuses up front and names both calls (fail closed)."""
    capsule, _ = env.capture(level=None)
    outer = _by_name(env.read(capsule / "tool-calls.jsonl"))["ask_helper"]
    assert outer["result"] is None
    result, _, cli = env.replay(capsule)
    assert cli.exit_code == 1, cli.output
    assert result["error"]["type"] == "ToolSurfaceNotServed"
    listed = result["error"]["tool_calls_not_served_by_surface"][OAI]["tools"]
    assert listed == ["ask_helper", "lookup"]
    assert env.side_effects == []


def test_a_record_captured_before_r3c_is_served_without_a_recapture(
    env: Env, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wrapped agent tool earlier capture marked not servable for nesting alone."""
    from novafabric.capture import _tool_codec

    monkeypatch.setenv("AGENT_WRAP", "1")
    capsule, captured = env.capture()
    path = capsule / "tool-calls.jsonl"
    records = env.read(path)
    for rec in records:
        if rec["tool_name"] == "ask_helper":
            ext = rec["extensions"]
            ext["io.novafabric.result_codec"] = "not-servable"
            ext["io.novafabric.not_servable_reason"] = (
                _tool_codec.seam_nested_reason("Agents SDK tool", ext[NESTED])
            )
    env.write(path, records)
    result, observed, cli = env.replay(capsule)
    assert cli.exit_code == 0, cli.output
    assert result["status"] == "success", result.get("error")
    assert observed == captured
    assert result["replay_contract"]["tool_calls_covered"] == 1


def test_an_unservable_observed_agent_tool_refuses_up_front_and_runs_nothing(
    env: Env,
) -> None:
    capsule, _ = env.capture()
    path = capsule / "tool-calls.jsonl"
    records = env.read(path)
    for rec in records:
        if rec["tool_name"] == "ask_helper":
            rec["extensions"]["io.novafabric.result_codec"] = "not-servable"
            rec["extensions"]["io.novafabric.not_servable_reason"] = (
                "the Agent.as_tool call sets on_stream: serving it would drop that "
                "effect of its sub-run"
            )
            rec["result"] = None
    env.write(path, records)
    result, _, cli = env.replay(capsule)
    assert cli.exit_code == 1, cli.output
    assert result["error"]["type"] == "ToolSurfaceNotServed"
    # Only the outer call is listed: the inner lookup is not covered (the outer is
    # not served), and it is servable on its own.
    assert result["error"]["tool_calls_not_served_by_surface"][OAI]["tools"] == ["ask_helper"]
    assert env.side_effects == []
