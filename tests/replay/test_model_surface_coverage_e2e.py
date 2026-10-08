"""ADR-0304 / issue #16 scenarios 14-16: async, streaming and Responses API calls
round-trip through ``nova capture`` and mocked ``nova replay``.

Each test captures a real agent process (the real ``openai`` SDK over an
``httpx.MockTransport`` that serves canned JSON or server-sent events; a fake
``anthropic`` package, since the real SDK is not a dependency; a real in-memory
MCP server whose tools write a side-effect log), then replays the capsule with the
network refused. What the agent observed during replay must equal what it observed
during capture -- for a stream, what a consumer folds the chunks/events into --
the replay must succeed with every recorded response consumed, and no MCP tool
may run live.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from _mocked_replay_agent import (
    anthropic_events,
    openai_body,
    openai_chunks,
    responses_body,
    responses_sse,
    write_agent,
    write_fake_anthropic,
)
from typer.testing import CliRunner

from novafabric.cli.main import app

pytest.importorskip("openai")
pytest.importorskip("mcp")

runner = CliRunner()

TOOL = {"op": "tool", "name": "get_weather", "args": {"city": "Paris"}}
CALL = ("call_A", "get_weather", {"city": "Paris"})


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


def _records(capsule: Path) -> list[dict[str, Any]]:
    path = capsule / "model-calls.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _sdk_records(capsule: Path) -> list[dict[str, Any]]:
    """The SDK-hook records (the wire hook's duplicates carry no choices)."""
    return [r for r in _records(capsule) if r.get("gen_ai.response.choices")]


def _round_trip(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch, plan: list[dict[str, Any]],
    *, canned: list[Any] | None = None, anthropic: list[Any] | None = None,
) -> tuple[Path, list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    monkeypatch.setenv("AGENT_PLAN", json.dumps(plan))
    if canned is not None:
        monkeypatch.setenv("AGENT_CANNED", json.dumps(canned))
    if anthropic is not None:
        fake = write_fake_anthropic(env["agent"].parent / "fake_sdk")
        monkeypatch.setenv("PYTHONPATH", str(fake))
        monkeypatch.setenv("FAKE_ANTHROPIC_CANNED", json.dumps(anthropic))

    result = runner.invoke(
        app, ["capture", "--output-dir", str(env["runs"]), sys.executable, str(env["agent"])]
    )
    assert result.exit_code == 0, result.output
    (capsule,) = [p for p in env["runs"].iterdir() if (p / "capsule.yaml").exists()]
    captured = json.loads(env["out"].read_text())
    assert all("error" not in o for o in captured), captured

    # Replay with the network refused and every MCP tool watched.
    monkeypatch.delenv("AGENT_CANNED", raising=False)
    monkeypatch.delenv("FAKE_ANTHROPIC_CANNED", raising=False)
    env["out"].unlink()
    if env["side"].exists():
        env["side"].unlink()
    result = runner.invoke(app, ["replay", str(capsule), "-o", str(env["replays"])])
    (replay_dir,) = list(env["replays"].iterdir())
    replay = yaml.safe_load((replay_dir / "replay_result.yaml").read_text())
    assert result.exit_code == 0, (result.output, replay)
    replayed = json.loads(env["out"].read_text())
    return capsule, captured, replay, replayed


def _assert_faithful(
    replay: dict[str, Any], captured: list[Any], replayed: list[Any], env: dict[str, Path],
    *, model_calls: int, tool_calls: int = 0,
) -> None:
    assert replayed == captured
    assert not env["side"].exists(), "an MCP tool executed live during mocked replay"
    assert replay["status"] == "success"
    assert replay["model_calls_mocked"] == replay["model_calls_available"] == model_calls
    assert replay["tool_calls_mocked"] == tool_calls
    assert replay["queues_fully_consumed"] is True
    assert replay["replay_contract"]["model_calls_live"] == 0
    assert "divergence_reason" not in replay


# ── scenario 14: async ───────────────────────────────────────────────────────


def test_s14_async_chat_completions_round_trip(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    capsule, captured, replay, replayed = _round_trip(
        env, monkeypatch, [{"op": "async_chat"}, TOOL, {"op": "async_chat"}],
        canned=[openai_body(tool_calls=[CALL], rid="r1"),
                openai_body(content="Paris is 22C", rid="r2")],
    )
    records = _sdk_records(capsule)
    assert [r["gen_ai.response.id"] for r in records] == ["r1", "r2"]
    assert records[0]["gen_ai.response.choices"][0]["message"]["tool_calls"] == [
        {"id": "call_A", "name": "get_weather", "arguments": {"city": "Paris"}}
    ]
    assert replayed[0]["tool_calls"][0]["id"] == "call_A"
    assert replayed[2]["content"] == "Paris is 22C"
    _assert_faithful(replay, captured, replayed, env, model_calls=2, tool_calls=1)


def test_s14_async_anthropic_messages_round_trip(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    capsule, captured, replay, replayed = _round_trip(
        env, monkeypatch, [{"op": "anthropic_async"}, {"op": "anthropic_async"}],
        anthropic=[
            {"id": "msg_1", "model": "claude-x", "stop_reason": "tool_use", "content": [
                {"type": "tool_use", "id": "toolu_1", "name": "get_weather",
                 "input": {"city": "Paris"}}]},
            {"id": "msg_2", "model": "claude-x", "stop_reason": "end_turn",
             "content": [{"type": "text", "text": "Done."}]},
        ],
    )
    assert [r["gen_ai.response.id"] for r in _sdk_records(capsule)] == ["msg_1", "msg_2"]
    assert replayed[0]["stop_reason"] == "tool_use"
    _assert_faithful(replay, captured, replayed, env, model_calls=2)


# ── scenario 15: streaming ───────────────────────────────────────────────────


def test_s15_streamed_chat_completions_round_trip(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    capsule, captured, replay, replayed = _round_trip(
        env, monkeypatch,
        [{"op": "stream_chat", "usage": True}, TOOL, {"op": "stream_chat"}],
        canned=[openai_chunks(tool_calls=[CALL, ("call_B", "get_weather", {"city": "Rome"})],
                              usage=True, rid="s1"),
                openai_chunks(content="Paris 22, Rome 25", rid="s2")],
    )
    first, second = _sdk_records(capsule)
    # The chunks were folded into one canonical record per call.
    assert first["gen_ai.response.choices"][0]["message"]["tool_calls"] == [
        {"id": "call_A", "name": "get_weather", "arguments": {"city": "Paris"}},
        {"id": "call_B", "name": "get_weather", "arguments": {"city": "Rome"}},
    ]
    assert first["gen_ai.response.choices"][0]["finish_reason"] == "tool_calls"
    assert first["gen_ai.usage.output_tokens"] == 5
    assert first["nova.streaming"]["streamed"] is True
    assert first["nova.streaming"]["chunk_count"] >= 5
    assert "io.novafabric.stream_complete" not in first.get("extensions", {})
    assert second["gen_ai.response.choices"][0]["message"]["content"] == "Paris 22, Rome 25"
    assert captured[0]["usage"] == 8  # the usage chunk is served back when requested
    _assert_faithful(replay, captured, replayed, env, model_calls=2, tool_calls=1)


def test_s15_async_streamed_chat_round_trip(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, captured, replay, replayed = _round_trip(
        env, monkeypatch, [{"op": "async_stream_chat"}],
        canned=[openai_chunks(content="streamed hello")],
    )
    assert replayed[0]["content"] == "streamed hello"
    _assert_faithful(replay, captured, replayed, env, model_calls=1)


def test_s15_chat_stream_helper_round_trip(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``chat.completions.stream()`` goes through ``create(stream=True)``; the SDK's
    own stream accumulator must work on the replayed chunks."""
    _, captured, replay, replayed = _round_trip(
        env, monkeypatch, [{"op": "chat_stream_helper"}],
        canned=[openai_chunks(tool_calls=[CALL])],
    )
    assert replayed[0]["tool_calls"] == [
        {"id": "call_A", "name": "get_weather", "arguments": {"city": "Paris"}}
    ]
    _assert_faithful(replay, captured, replayed, env, model_calls=1)


def test_s15_an_abandoned_stream_is_recorded_as_delivered_and_flagged(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    capsule, captured, replay, replayed = _round_trip(
        env, monkeypatch, [{"op": "stream_chat_partial"}],
        canned=[openai_chunks(content="abcdefghi", split=3)],
    )
    (record,) = _sdk_records(capsule)
    assert record["extensions"]["io.novafabric.stream_complete"] is False
    # Only what the workload read before closing: the role chunk (empty content).
    assert record["gen_ai.response.choices"][0]["message"]["content"] == ""
    _assert_faithful(replay, captured, replayed, env, model_calls=1)


def test_s15_streamed_anthropic_messages_round_trip(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    capsule, captured, replay, replayed = _round_trip(
        env, monkeypatch, [{"op": "anthropic_stream"}, TOOL, {"op": "anthropic_async_stream"}],
        anthropic=[
            anthropic_events("Checking the weather.",
                             ("toolu_1", "get_weather", {"city": "Paris"}),
                             stop_reason="tool_use", mid="msg_1"),
            anthropic_events("It is 22C.", mid="msg_2"),
        ],
    )
    first, second = _sdk_records(capsule)
    message = first["gen_ai.response.choices"][0]["message"]
    assert message["content"] == "Checking the weather."
    assert message["tool_calls"] == [
        {"id": "toolu_1", "name": "get_weather", "arguments": {"city": "Paris"}}
    ]
    assert first["extensions"]["io.novafabric.provider_finish_reasons"] == ["tool_use"]
    assert first["nova.streaming"]["streamed"] is True
    assert replayed[0]["stop_reason"] == "tool_use"
    assert replayed[2]["blocks"][0]["text"] == "It is 22C."
    _assert_faithful(replay, captured, replayed, env, model_calls=2, tool_calls=1)


# ── scenario 16: OpenAI Responses API ────────────────────────────────────────


def test_s16_responses_api_round_trip_with_function_call(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    capsule, captured, replay, replayed = _round_trip(
        env, monkeypatch, [{"op": "responses"}, TOOL, {"op": "async_responses"}],
        canned=[responses_body(calls=[CALL], rid="resp_1"),
                responses_body(text="Paris is 22C", rid="resp_2")],
    )
    first, second = _sdk_records(capsule)
    assert first["extensions"]["io.novafabric.api_surface"] == "openai.responses"
    assert first["gen_ai.response.choices"][0]["message"]["tool_calls"] == [
        {"id": "call_A", "name": "get_weather", "arguments": {"city": "Paris"}}
    ]
    assert first["gen_ai.response.choices"][0]["finish_reason"] == "tool_calls"
    assert second["gen_ai.response.choices"][0]["message"]["content"] == "Paris is 22C"
    assert replayed[0]["calls"] == [
        {"call_id": "call_A", "name": "get_weather", "arguments": {"city": "Paris"}}
    ]
    assert replayed[2]["text"] == "Paris is 22C"
    _assert_faithful(replay, captured, replayed, env, model_calls=2, tool_calls=1)


def test_s16_streamed_responses_api_round_trip(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    body = responses_body(text="streamed responses answer")
    _, captured, replay, replayed = _round_trip(
        env, monkeypatch,
        [{"op": "stream_responses"}, {"op": "async_stream_responses"},
         {"op": "responses_stream_helper"}],
        canned=[responses_sse(body), responses_sse(body), responses_sse(body)],
    )
    assert replayed[0]["delta_text"] == replayed[0]["text"] == "streamed responses answer"
    _assert_faithful(replay, captured, replayed, env, model_calls=3)


def test_mixed_surfaces_are_served_in_recorded_order(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, captured, replay, replayed = _round_trip(
        env, monkeypatch,
        [{"op": "chat"}, {"op": "responses"}, {"op": "stream_chat"}, {"op": "async_chat"}],
        canned=[openai_body(content="one"), responses_body(text="two"),
                openai_chunks(content="three"), openai_body(content="four")],
    )
    assert [o.get("content") or o.get("text") for o in replayed] == [
        "one", "two", "three", "four"
    ]
    _assert_faithful(replay, captured, replayed, env, model_calls=4)
