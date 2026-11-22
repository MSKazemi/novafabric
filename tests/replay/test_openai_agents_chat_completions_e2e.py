"""An OpenAI Agents SDK ``Agent`` on ``OpenAIChatCompletionsModel`` through real
``nova capture`` and strict mocked ``nova replay`` subprocesses.

That model does ``isinstance(ret, ChatCompletion)`` on what
``chat.completions.create`` returns and treats anything else as a stream. Mocked
replay used to serve a non-streamed Chat Completion as ``types.SimpleNamespace``,
so the first served model call crashed the run. It now serves the SDK's own
``ChatCompletion``.

The workload is a real ``agents`` ``Runner`` over the real ``openai`` SDK with an
``httpx.MockTransport`` (no network, no API key).
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

AGENT = '''
import asyncio, json, os

import httpx
from openai import AsyncOpenAI
from agents import Agent, OpenAIChatCompletionsModel, Runner, function_tool

SIDE = os.environ["AGENT_SIDE"]
OUT = os.environ["AGENT_OUT"]
CANNED = json.loads(os.environ.get("AGENT_CANNED") or "null")
SERVED = [0]


def transport(request):
    if CANNED is None:
        raise RuntimeError("network reached during replay")
    body = CANNED[SERVED[0]]
    SERVED[0] += 1
    return httpx.Response(200, json=body)


CLIENT = AsyncOpenAI(api_key="sk-test", max_retries=0,
                     http_client=httpx.AsyncClient(transport=httpx.MockTransport(transport)))
MODEL = OpenAIChatCompletionsModel(model="gpt-4o", openai_client=CLIENT)


@function_tool
def lookup(order_id: str) -> dict:
    """Look up an order."""
    with open(SIDE, "a") as fh:
        fh.write(f"lookup {order_id}\\n")
    return {"status": "shipped", "id": order_id}


async def main():
    agent = Agent(name="agent", model=MODEL, tools=[lookup])
    out = []
    try:
        result = await Runner.run(agent, "where is order 7?")
        for item in result.new_items:
            if item.type == "tool_call_output_item":
                out.append({"tool_output": str(item.output)})
        out.append({"final": result.final_output})
    except BaseException as exc:  # the workload swallows every failure
        out.append({"error": type(exc).__name__, "message": str(exc)[:300]})
    with open(OUT, "w") as fh:
        json.dump(out, fh)


asyncio.run(main())
'''


def _canned() -> list[dict[str, Any]]:
    from _mocked_replay_agent import openai_body

    return [
        openai_body(tool_calls=[("call_l", "lookup", {"order_id": "7"})], rid="cc-1"),
        openai_body(content="Order 7 has shipped.", rid="cc-2"),
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
        for var in ("FORCE_COLOR", "COLORTERM", "AGENT_CANNED", "NOVA_CAPTURE_LEVEL"):
            monkeypatch.delenv(var, raising=False)

    def capture(self) -> tuple[Path, list[dict[str, Any]]]:
        self._mp.setenv("NOVA_CAPTURE_LEVEL", "forensic")
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

    def replay(self, capsule: Path) -> tuple[dict[str, Any], list[Any], Any]:
        cli = runner.invoke(app, ["replay", str(capsule), "-o", str(self.replays)])
        (replay_dir,) = list(self.replays.iterdir())
        result = yaml.safe_load((replay_dir / "replay_result.yaml").read_text())
        jsonschema.validate(result, _RESULT_SCHEMA)
        observed = json.loads(self.out.read_text()) if self.out.exists() else []
        return result, observed, cli

    @property
    def side_effects(self) -> list[str]:
        return self.side.read_text().splitlines() if self.side.exists() else []


def test_strict_mocked_replay_serves_a_chat_completions_model_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = Env(tmp_path, monkeypatch)
    capsule, captured = env.capture()
    assert captured[-1] == {"final": "Order 7 has shipped."}, captured
    assert all("error" not in item for item in captured), captured

    result, observed, cli = env.replay(capsule)  # strict, AGENT_CANNED unset
    assert cli.exit_code == 0, cli.output
    assert result["status"] == "success", result.get("error")
    assert observed == captured  # no crash on the first served call
    assert result["model_calls_mocked"] == 2
    contract = result["replay_contract"]
    assert contract["model_calls_live"] == 0
    assert contract["model_calls_unconsumed"] == 0
    assert contract["divergences"] == []
    assert result["tool_calls_mocked"] == 1 and result["tool_calls_live"] == 0
    assert env.side_effects == [], "the recorded tool ran again"
    assert result["fidelity"]["verdict"] == "faithful", result["fidelity"]
