"""A Google ADK ``AgentTool`` (a sub-agent used as a tool) through real ``nova
capture`` and mocked ``nova replay`` subprocesses (ADR-0306 r3c).

Before r3c the ADK seam marked a call whose body made model or tool calls not
servable ("nested-call coverage is not implemented"), so a strict replay of an
``AgentTool`` diverged at the call. Now the LangChain model (r3b) applies: the
outer call is served from the capsule, its body -- the sub-agent's run -- never
runs, and the sub-agent's model and tool calls are consumed as *covered*.

The workload is a real ``google.adk`` ``Runner`` whose agents use a ``BaseLlm``
subclass that calls the real ``openai`` SDK over an ``httpx.MockTransport`` (no
network, no API key), so every model call is recorded and served. The NovaFabric
ADK tool plugin is first in the root runner's plugins; ``AgentTool`` passes the
parent's plugins to its sub-runner (``include_plugins=True``, the default), so
the sub-agent's tool calls go through the same seam.
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

pytest.importorskip("google.adk")
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

ADK = "google.adk.tools.BaseTool"
WITHIN = "io.novafabric.within_tool_call_id"
NESTED = "io.novafabric.nested_records"

AGENT = '''
import asyncio, json, os
from typing import AsyncGenerator

import httpx
from openai import AsyncOpenAI
from google.adk.agents import LlmAgent
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools.agent_tool import AgentTool
from google.genai import types

from novafabric.adapters.google_adk import make_tool_plugin

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


CLIENT = AsyncOpenAI(api_key="sk-test", max_retries=0,
                     http_client=httpx.AsyncClient(transport=httpx.MockTransport(transport)))


def to_openai(contents):
    out = []
    for c in contents:
        for p in c.parts or []:
            if p.function_call is not None:
                fc = p.function_call
                out.append({"role": "assistant", "content": None, "tool_calls": [
                    {"id": fc.id, "type": "function",
                     "function": {"name": fc.name, "arguments": json.dumps(fc.args or {})}}]})
            elif p.function_response is not None:
                fr = p.function_response
                out.append({"role": "tool", "tool_call_id": fr.id,
                            "content": json.dumps(fr.response)})
            elif p.text:
                out.append({"role": "user" if c.role == "user" else "assistant",
                            "content": p.text})
    return out


class OpenAIBackedLlm(BaseLlm):
    """An ADK model over the openai SDK (no network: a mock transport)."""

    model: str = "gpt-4o"

    async def generate_content_async(
        self, llm_request, stream=False
    ) -> AsyncGenerator[LlmResponse, None]:
        r = await CLIENT.chat.completions.create(
            model="gpt-4o", messages=to_openai(llm_request.contents))
        msg = r.choices[0].message
        parts = [types.Part(function_call=types.FunctionCall(
            name=c.function.name, args=json.loads(c.function.arguments), id=c.id))
            for c in (msg.tool_calls or [])]
        if msg.content:
            parts.append(types.Part(text=msg.content))
        yield LlmResponse(content=types.Content(role="model", parts=parts))


def lookup(order_id: str) -> dict:
    """Look up an order (called by the sub-agent only)."""
    side(f"lookup {order_id}")
    return {"status": "shipped", "id": order_id}


researcher = LlmAgent(
    name="researcher", model=OpenAIBackedLlm(), tools=[lookup],
    description="Finds out about an order.",
)


async def main():
    agent = LlmAgent(name="agent", model=OpenAIBackedLlm(), tools=[AgentTool(researcher)])
    svc = InMemorySessionService()
    adk_runner = Runner(
        app_name="nova-adk-nested", agent=agent, session_service=svc,
        plugins=[make_tool_plugin()],
    )
    session = await svc.create_session(app_name="nova-adk-nested", user_id="u")
    out = []
    try:
        async for event in adk_runner.run_async(
            user_id="u", session_id=session.id,
            new_message=types.Content(role="user", parts=[types.Part(text="where is 7?")]),
        ):
            for part in (event.content.parts if event.content else None) or []:
                if part.function_response is not None:
                    fr = part.function_response
                    out.append({"name": fr.name, "response": fr.response})
                elif part.text and event.author == "agent":
                    out.append({"final": part.text})
    except BaseException as exc:  # the workload swallows every failure
        cause = exc.__cause__
        out.append({"error": type(exc).__name__, "message": str(exc)[:300],
                    "cause": type(cause).__name__ if cause is not None else None})
    with open(OUT, "w") as fh:
        json.dump(out, fh)


asyncio.run(main())
'''


def _canned() -> list[dict[str, Any]]:
    from _mocked_replay_agent import openai_body

    return [
        openai_body(tool_calls=[("call_r", "researcher", {"request": "find order 7"})],
                    rid="outer-1"),
        openai_body(tool_calls=[("call_l", "lookup", {"order_id": "7"})], rid="sub-1"),
        openai_body(content="order 7 has shipped", rid="sub-2"),
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
        for var in ("FORCE_COLOR", "COLORTERM", "AGENT_CANNED", "NOVA_CAPTURE_LEVEL"):
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


def test_capture_marks_the_sub_agent_calls_inside_the_agent_tool(env: Env) -> None:
    capsule, captured = env.capture()
    assert captured[-1] == {"final": "Order 7 has shipped."}
    assert captured[0]["name"] == "researcher"
    tools = _by_name(env.read(capsule / "tool-calls.jsonl"))
    assert set(tools) == {"researcher", "lookup"}
    for rec in tools.values():
        jsonschema.validate(rec, _TOOL_SCHEMA)
    outer, inner = tools["researcher"], tools["lookup"]
    assert outer["extensions"]["io.novafabric.adk_tool_class"].endswith("AgentTool")
    # The sub-runner inherited the plugin, and the boundary followed the call
    # into it: the sub-agent's tool call and model calls are marked inside it.
    assert inner["extensions"][WITHIN] == outer["tool_call_id"]
    models = [
        r for r in env.read(capsule / "model-calls.jsonl")
        if (r.get("extensions") or {}).get("io.novafabric.record_role") == "logical"
    ]
    inside = [m for m in models if (m.get("extensions") or {}).get(WITHIN)]
    assert len(models) == 4 and len(inside) == 2
    assert {m["extensions"][WITHIN] for m in inside} == {outer["tool_call_id"]}
    assert outer["extensions"][NESTED] >= 3
    # Nesting no longer blocks serving (ADR-0306 r3c).
    assert outer["extensions"]["io.novafabric.result_codec"] == "json-v1", outer
    assert "io.novafabric.not_servable_reason" not in outer["extensions"]


def test_strict_replay_serves_the_agent_tool_and_covers_the_sub_agent(env: Env) -> None:
    capsule, captured = env.capture()
    result, observed, cli = env.replay(capsule)  # strict, AGENT_CANNED unset
    assert cli.exit_code == 0, cli.output
    assert result["status"] == "success", result.get("error")
    assert env.side_effects == [], "the sub-agent's tool ran"
    assert observed == captured
    contract = result["replay_contract"]
    assert contract["divergences"] == []
    assert result["tool_calls_mocked"] == 1  # only the AgentTool call was asked for
    assert result["tool_calls_live"] == 0
    assert contract["tool_calls_covered"] == 1  # the sub-agent's lookup
    assert contract["tool_calls_by_surface"][ADK]["covered"] == 1
    assert contract["tool_calls_unconsumed"] == 0
    assert result["model_calls_mocked"] == 2  # the root agent's two calls
    assert contract["model_calls_covered"] == 2  # the sub-agent's two calls
    assert contract["model_calls_unconsumed"] == 0
    assert result["fidelity"]["verdict"] == "faithful", result["fidelity"]

    dry, _, cli = env.replay(capsule, "--dry-run")
    assert cli.exit_code == 0, cli.output
    report = env.report(dry)
    assert "[MOCK] researcher" in report
    assert "[COVERED by researcher] lookup" in report


def test_an_unservable_inner_tool_inside_a_served_agent_tool_does_not_refuse(
    env: Env,
) -> None:
    capsule, captured = env.capture()
    path = capsule / "tool-calls.jsonl"
    records = env.read(path)
    for rec in records:
        if rec["tool_name"] == "lookup":
            rec["extensions"]["io.novafabric.result_codec"] = "not-servable"
            rec["extensions"]["io.novafabric.not_servable_reason"] = "the result is a set"
            rec["result"] = None
    env.write(path, records)
    result, observed, cli = env.replay(capsule)
    assert cli.exit_code == 0, cli.output
    assert result["status"] == "success", result.get("error")
    assert observed == captured
    assert result["replay_contract"]["tool_calls_covered"] == 1
    assert env.side_effects == []


def test_a_record_captured_before_r3c_is_served_without_a_recapture(env: Env) -> None:
    """Earlier capture marked the AgentTool call not servable with a fixed nesting
    text and kept its value; that text was policy, so replay strips it."""
    from novafabric.capture import _tool_codec

    capsule, captured = env.capture()
    path = capsule / "tool-calls.jsonl"
    records = env.read(path)
    for rec in records:
        if rec["tool_name"] == "researcher":
            ext = rec["extensions"]
            ext["io.novafabric.result_codec"] = "not-servable"
            ext["io.novafabric.not_servable_reason"] = (
                _tool_codec.seam_nested_reason("ADK tool", ext[NESTED])
            )
    env.write(path, records)
    result, observed, cli = env.replay(capsule)
    assert cli.exit_code == 0, cli.output
    assert result["status"] == "success", result.get("error")
    assert observed == captured
    assert result["replay_contract"]["tool_calls_covered"] == 1
    assert env.side_effects == []


def test_an_agent_tool_that_is_not_served_runs_nothing_and_fails_closed(env: Env) -> None:
    """The outer result was not kept (default level, nothing echoed under its
    digest): the call is refused before the sub-agent runs."""
    capsule, _ = env.capture()
    path = capsule / "tool-calls.jsonl"
    records = env.read(path)
    for rec in records:
        if rec["tool_name"] == "researcher":
            rec["extensions"]["io.novafabric.result_codec"] = "not-servable"
            rec["extensions"]["io.novafabric.not_servable_reason"] = "the result is a set"
            rec["result"] = None
    env.write(path, records)
    result, _, _ = env.replay(capsule)
    assert env.side_effects == []
    assert result["status"] == "failure"
    kinds = [d["kind"] for d in result["replay_contract"]["divergences"]]
    assert "tool_result_not_servable" in kinds
    assert result["replay_contract"]["tool_calls_covered"] == 0
