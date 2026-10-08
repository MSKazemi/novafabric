"""Issue #12 / ADR-0300: capture -> capsule -> mocked replay, end to end, both providers.

``nova capture`` runs a real agent process (the real ``openai`` SDK over an
``httpx.MockTransport``; a fake ``anthropic`` package, since the real SDK is not a
dependency; a real in-memory MCP server whose tools write a side-effect log).
``nova replay`` then re-runs the same agent with the network refused. The agent's
observations during replay must equal its observations during capture -- the
same tool-call ids, names and arguments, in order, the same stop reasons, and the
same tool results -- and the side-effect log must stay empty, because every MCP
tool result is served from the capsule.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from _mocked_replay_agent import openai_body, write_agent, write_fake_anthropic
from typer.testing import CliRunner

from novafabric.cli.main import app

pytest.importorskip("openai")
pytest.importorskip("mcp")

runner = CliRunner()


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    paths = {
        "agent": write_agent(tmp_path),
        "out": tmp_path / "out.json",
        "side": tmp_path / "side.log",
        "runs": tmp_path / "runs",
        "replays": tmp_path / "replays",
    }
    monkeypatch.setenv("AGENT_OUT", str(paths["out"]))
    monkeypatch.setenv("AGENT_SIDE", str(paths["side"]))
    for var in ("FORCE_COLOR", "COLORTERM", "AGENT_CANNED", "FAKE_ANTHROPIC_CANNED"):
        monkeypatch.delenv(var, raising=False)
    return paths


def _records(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _capture(env: dict[str, Path]) -> tuple[Path, list[dict[str, Any]]]:
    result = runner.invoke(
        app, ["capture", "--output-dir", str(env["runs"]), sys.executable, str(env["agent"])]
    )
    assert result.exit_code == 0, result.output
    (capsule,) = [p for p in env["runs"].iterdir() if (p / "capsule.yaml").exists()]
    observed = json.loads(env["out"].read_text())
    return capsule, observed


def _replay(env: dict[str, Path], capsule: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    env["out"].unlink()
    if env["side"].exists():
        env["side"].unlink()
    result = runner.invoke(app, ["replay", str(capsule), "-o", str(env["replays"])])
    (replay_dir,) = list(env["replays"].iterdir())
    replay = yaml.safe_load((replay_dir / "replay_result.yaml").read_text())
    assert result.exit_code == 0, (result.output, replay)
    return replay, json.loads(env["out"].read_text())


def test_openai_tool_calls_round_trip_through_capture_and_mocked_replay(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = [("call_A", "get_weather", {"city": "Paris"}),
             ("call_B", "get_weather", {"city": "Rome"})]
    monkeypatch.setenv("AGENT_PLAN", json.dumps([
        {"op": "chat"},
        {"op": "tool", "name": "get_weather", "args": {"city": "Paris"}},
        {"op": "tool", "name": "get_weather", "args": {"city": "Rome"}},
        {"op": "chat"},
    ]))
    monkeypatch.setenv("AGENT_CANNED", json.dumps([
        openai_body(tool_calls=calls, rid="r1"),
        openai_body(content="Paris 22, Rome 25", rid="r2"),
    ]))
    capsule, captured = _capture(env)
    assert env["side"].read_text().count("get_weather") == 2  # tools ran live once

    # The capsule holds the canonical shape (issue #12), not OpenAI's wire shape.
    sdk_records = [r for r in _records(capsule / "model-calls.jsonl")
                   if r.get("gen_ai.response.choices")]
    assert sdk_records[0]["gen_ai.response.choices"][0]["message"]["tool_calls"] == [
        {"id": i, "name": n, "arguments": a} for i, n, a in calls
    ]

    monkeypatch.delenv("AGENT_CANNED")
    replay, replayed = _replay(env, capsule)

    assert replayed == captured
    assert replayed[0]["tool_calls"] == [
        {"id": i, "name": n, "arguments": a} for i, n, a in calls
    ]
    assert replayed[0]["finish"] == "tool_calls"
    assert not env["side"].exists(), "an MCP tool executed live during mocked replay"
    assert replay["status"] == "success"
    assert replay["model_calls_mocked"] == replay["model_calls_available"] == 2
    assert replay["tool_calls_mocked"] == replay["tool_calls_available"] == 2
    assert replay["queues_fully_consumed"] is True
    assert "divergence_reason" not in replay


def test_anthropic_tool_use_round_trips_through_capture_and_mocked_replay(
    env: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = write_fake_anthropic(tmp_path / "fake_sdk")
    monkeypatch.setenv("PYTHONPATH", str(fake))
    monkeypatch.setenv("AGENT_PLAN", json.dumps([
        {"op": "anthropic"},
        {"op": "tool", "name": "get_weather", "args": {"city": "Paris"}},
        {"op": "tool", "name": "write_file", "args": {"path": "/tmp/x", "text": "hi"}},
        {"op": "anthropic"},
    ]))
    monkeypatch.setenv("FAKE_ANTHROPIC_CANNED", json.dumps([
        {"id": "msg_1", "model": "claude-x", "stop_reason": "tool_use", "content": [
            {"type": "text", "text": "Checking."},
            {"type": "tool_use", "id": "toolu_1", "name": "get_weather",
             "input": {"city": "Paris"}},
            {"type": "tool_use", "id": "toolu_2", "name": "write_file",
             "input": {"path": "/tmp/x", "text": "hi"}},
        ]},
        {"id": "msg_2", "model": "claude-x", "stop_reason": "end_turn",
         "content": [{"type": "text", "text": "Done."}]},
    ]))
    capsule, captured = _capture(env)

    first = _records(capsule / "model-calls.jsonl")[0]
    choice = first["gen_ai.response.choices"][0]
    # Canonical finish reason in the schema enum; the raw value kept additively.
    assert choice["finish_reason"] == "tool_calls"
    assert first["gen_ai.response.finish_reasons"] == ["tool_calls"]
    assert first["extensions"]["io.novafabric.provider_finish_reasons"] == ["tool_use"]
    assert [tc["id"] for tc in choice["message"]["tool_calls"]] == ["toolu_1", "toolu_2"]

    monkeypatch.delenv("FAKE_ANTHROPIC_CANNED")
    replay, replayed = _replay(env, capsule)

    assert replayed == captured
    assert replayed[0]["stop_reason"] == "tool_use"
    assert [b["type"] for b in replayed[0]["blocks"]] == ["text", "tool_use", "tool_use"]
    assert replayed[0]["blocks"][2]["input"] == {"path": "/tmp/x", "text": "hi"}
    assert replayed[3]["stop_reason"] == "end_turn"
    assert not env["side"].exists(), "a mutating MCP tool executed live during mocked replay"
    assert replay["status"] == "success"
    assert replay["model_calls_mocked"] == 2
    assert replay["tool_calls_mocked"] == 2
    assert replay["queues_fully_consumed"] is True
