"""AWS Bedrock ``Converse`` / ``ConverseStream`` calls are recorded with their response
at capture and served by mocked replay (Bedrock ADR, experimental).

Each test captures a real agent process -- real ``boto3`` against an in-process fake
Bedrock server reached through ``AWS_ENDPOINT_URL_BEDROCK_RUNTIME``, dummy credentials,
region ``eu-central-1``, never real AWS -- then replays the capsule while the fake server
answers something else. The replay must observe exactly what the capture observed and
the server must receive no model request during the replay.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from _bedrock_agent import write_agent
from typer.testing import CliRunner

from novafabric.cli.main import app

pytest.importorskip("boto3")

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
    for var in ("FORCE_COLOR", "COLORTERM", "AWS_ENDPOINT_URL_BEDROCK_RUNTIME",
                "AWS_ENDPOINT_URL_BEDROCK", "AWS_ENDPOINT_URL", "AWS_PROFILE",
                "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "no-aws-config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "no-aws-credentials"))
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    return paths


def _records(capsule: Path) -> list[dict[str, Any]]:
    path = capsule / "model-calls.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _hits(env: dict[str, Path]) -> list[str]:
    return env["side"].read_text().splitlines() if env["side"].exists() else []


def _capture(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch, plan: list[dict[str, Any]]
) -> tuple[Path, list[dict[str, Any]], list[str]]:
    monkeypatch.setenv("AGENT_PLAN", json.dumps(plan))
    monkeypatch.setenv("BEDROCK_CANNED", "capture-answer")
    monkeypatch.setenv("BEDROCK_TOOL_RESULT", "sunny")
    result = runner.invoke(
        app, ["capture", "--output-dir", str(env["runs"]), sys.executable, str(env["agent"])]
    )
    assert result.exit_code == 0, result.output
    (capsule,) = [p for p in env["runs"].iterdir() if (p / "capsule.yaml").exists()]
    captured = json.loads(env["out"].read_text())
    hits = _hits(env)
    env["out"].unlink()
    env["side"].unlink()
    return capsule, captured, hits


def _replay(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch, capsule: Path,
    *, plan: list[dict[str, Any]] | None = None, args: tuple[str, ...] = (),
    tool_result: str = "sunny",
) -> tuple[Any, dict[str, Any], list[dict[str, Any]] | None]:
    if plan is not None:
        monkeypatch.setenv("AGENT_PLAN", json.dumps(plan))
    monkeypatch.setenv("BEDROCK_CANNED", "REPLAY-LIVE-ANSWER")
    monkeypatch.setenv("BEDROCK_TOOL_RESULT", tool_result)
    # Other credentials than at capture: a different SigV4 signature and date on every
    # request, which must not matter to serving.
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIDREPLAYOTHER")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "another-fake-secret")
    result = runner.invoke(app, ["replay", str(capsule), "-o", str(env["replays"]), *args])
    (replay_dir,) = list(env["replays"].iterdir())
    replay = yaml.safe_load((replay_dir / "replay_result.yaml").read_text())
    replayed = json.loads(env["out"].read_text()) if env["out"].exists() else None
    return result, replay, replayed


def _assert_faithful(
    env: dict[str, Path], result: Any, replay: dict[str, Any],
    captured: list[Any], replayed: list[Any] | None, *, model_calls: int,
) -> None:
    assert result.exit_code == 0, (result.output, replay)
    assert replayed == captured
    assert _hits(env) == [], "the fake Bedrock server was hit during mocked replay"
    assert replay["status"] == "success"
    assert replay["model_calls_mocked"] == replay["model_calls_available"] == model_calls
    assert replay["queues_fully_consumed"] is True
    contract = replay["replay_contract"]
    assert contract["model_calls_live"] == 0
    assert contract["network_connections_live"] == 0
    assert contract["model_calls_not_servable_by_system"] == {}
    assert "divergence_reason" not in replay


def test_converse_and_converse_stream_round_trip(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = [{"op": "converse"}, {"op": "stream"}]
    capsule, captured, hits = _capture(env, monkeypatch, plan)
    assert [h.split()[2] for h in hits] == [
        "/model/qwen.qwen3-32b-v1:0/converse", "/model/qwen.qwen3-32b-v1:0/converse-stream",
    ]
    assert all("signed=True" in h for h in hits)
    assert captured[0]["output"]["message"]["content"] == [{"text": "capture-answer #1"}]

    first, second = _records(capsule)
    for record in (first, second):
        assert record["gen_ai.system"] == "aws.bedrock"
        assert record["gen_ai.request.model"] == "qwen.qwen3-32b-v1:0"
        assert record["gen_ai.request.max_tokens"] == 64
        assert record["gen_ai.request.temperature"] == 0.0
        assert record["gen_ai.request.top_p"] == 0.9
        assert record["gen_ai.request.messages"] == [
            {"role": "user", "content": [{"text": "hi"}]}
        ]
        assert record["extensions"]["io.novafabric.bedrock.request"]["system"] == [
            {"text": "be brief"}
        ]
        assert record["extensions"]["io.novafabric.record_role"] == "logical"
        assert record["gen_ai.usage.input_tokens"] == 7
        assert "authorization" not in json.dumps(record).lower()
    assert first["extensions"]["io.novafabric.api_surface"] == "aws.bedrock.converse"
    assert second["extensions"]["io.novafabric.api_surface"] == "aws.bedrock.converse_stream"
    assert first["gen_ai.response.choices"] == [{
        "index": 0, "message": {"role": "assistant", "content": "capture-answer #1"},
        "finish_reason": "stop",
    }]
    assert first["extensions"]["io.novafabric.provider_finish_reasons"] == ["end_turn"]
    assert second["gen_ai.response.choices"][0]["message"]["content"] == "capture-answer #2"
    assert second["nova.streaming"]["chunk_count"] == 6
    assert "io.novafabric.stream_complete" not in second["extensions"]

    result, replay, replayed = _replay(env, monkeypatch, capsule)
    _assert_faithful(env, result, replay, captured, replayed, model_calls=2)


def test_a_tool_use_turn_and_its_tool_result_round_trip(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = [
        {"op": "converse", "tools": True, "tool_result": "x"},
        {"op": "stream", "tools": True, "tool_result": "x"},
    ]
    capsule, captured, _ = _capture(env, monkeypatch, plan)
    tool_use = captured[0]["first"]["output"]["message"]["content"][0]["toolUse"]
    assert tool_use == {"toolUseId": "tooluse_capture-answer_1", "name": "get_weather",
                        "input": {"city": "capture-answer"}}
    records = _records(capsule)
    assert len(records) == 4
    for record in (records[0], records[2]):
        (choice,) = record["gen_ai.response.choices"]
        assert choice["finish_reason"] == "tool_calls"
        (call,) = choice["message"]["tool_calls"]
        assert call["name"] == "get_weather" and call["arguments"] == {"city": "capture-answer"}
        assert call["id"].startswith("tooluse_capture-answer_")
        assert record["extensions"]["io.novafabric.bedrock.request"]["toolConfig"]["tools"][0][
            "toolSpec"]["name"] == "get_weather"

    result, replay, replayed = _replay(env, monkeypatch, capsule)
    _assert_faithful(env, result, replay, captured, replayed, model_calls=4)
    echo = replay["replay_contract"]["tool_result_echo"]
    assert echo["matched"] == 2 and echo["mismatched"] == 0


def test_a_different_tool_result_is_reported_by_the_echo_check(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The replayed workload sends back another tool result than at capture: the
    report-only echo check (ADR-0306 D10) names it; serving stays positional."""
    plan = [{"op": "converse", "tools": True, "tool_result": "x"}]
    capsule, captured, _ = _capture(env, monkeypatch, plan)
    result, replay, replayed = _replay(env, monkeypatch, capsule, tool_result="STORMY")
    assert result.exit_code == 0, result.output
    assert replayed == captured
    echo = replay["replay_contract"]["tool_result_echo"]
    assert echo["mismatched"] == 1, echo
    (mismatch,) = echo["mismatches"]
    assert mismatch["tool_call_id"] == "tooluse_capture-answer_1"
    assert mismatch["surface"] == "aws.bedrock converse"
    assert "STORMY" not in json.dumps(replay) and "sunny" not in json.dumps(replay)


def test_an_abandoned_stream_is_recorded_as_such_and_replayed_as_delivered(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = [{"op": "stream", "abandon": True}, {"op": "converse"}]
    capsule, captured, _ = _capture(env, monkeypatch, plan)
    assert captured[0]["events"] == [{"messageStart": {"role": "assistant"}}]
    first = _records(capsule)[0]
    assert first["extensions"]["io.novafabric.stream_complete"] is False
    result, replay, replayed = _replay(env, monkeypatch, capsule)
    _assert_faithful(env, result, replay, captured, replayed, model_calls=2)


def test_a_recorded_http_error_and_a_throttled_retry_are_served_as_recorded(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """botocore retries a 429 at capture: two attempts, two records. Replay serves the
    429 back, so botocore retries exactly as it did and gets the recorded success; a
    400 is served back and botocore raises its own ``ValidationException``."""
    plan = [
        {"op": "converse", "model": "throttle-once"},
        {"op": "converse", "model": "bad-model", "catch": True},
    ]
    capsule, captured, hits = _capture(env, monkeypatch, plan)
    assert len(hits) == 3
    assert captured[1]["error"] == "ValidationException"
    records = _records(capsule)
    assert [r["status"] for r in records] == ["error", "success", "error"]
    assert records[0]["error"]["type"] == "ThrottlingException"
    assert records[2]["error"]["type"] == "ValidationException"
    result, replay, replayed = _replay(env, monkeypatch, capsule)
    _assert_faithful(env, result, replay, captured, replayed, model_calls=3)


def test_bedrock_queue_exhaustion_fails_closed(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    capsule, _, _ = _capture(env, monkeypatch, [{"op": "converse"}])
    plan = [{"op": "converse"}, {"op": "converse", "catch": True}]
    result, replay, replayed = _replay(env, monkeypatch, capsule, plan=plan)
    assert replayed is not None
    assert replayed[0]["output"]["message"]["content"] == [{"text": "capture-answer #1"}]
    # botocore wraps any exception its urllib3 call raises in HTTPClientError; the
    # divergence itself is in the replay result.
    assert replayed[1]["error"] == "HTTPClientError"
    assert "no recorded aws.bedrock response left" in replayed[1]["message"]
    assert _hits(env) == [], "an exhausted Bedrock queue went to the live server"
    assert replay["status"] == "failure"
    assert "model_queue_exhausted" in replay["divergence_reason"]


def test_converse_stream_where_converse_was_recorded_fails_closed(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    capsule, _, _ = _capture(env, monkeypatch, [{"op": "converse"}])
    result, replay, replayed = _replay(
        env, monkeypatch, capsule, plan=[{"op": "stream", "catch": True}]
    )
    assert replayed is not None and replayed[0]["error"] == "HTTPClientError"
    assert "aws.bedrock converse-stream" in replayed[0]["message"]
    assert _hits(env) == []
    assert replay["status"] == "failure"
    assert "provider_mismatch" in replay["divergence_reason"]


def test_invoke_model_is_recorded_without_a_response_and_refused(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """InvokeModel bodies are provider-specific: recorded without a response, so a
    strict replay refuses before the workload starts."""
    capsule, _, _ = _capture(env, monkeypatch, [{"op": "invoke"}])
    (record,) = _records(capsule)
    assert record["extensions"]["io.novafabric.response_not_recorded"].startswith(
        "InvokeModel"
    )
    result, replay, replayed = _replay(env, monkeypatch, capsule)
    assert replay["status"] == "aborted"
    assert replay["error"]["type"] == "ModelSurfaceNotServable"
    assert replayed is None and _hits(env) == []


def test_a_control_plane_call_runs_no_model_and_is_not_refused(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``ListFoundationModels`` (the ``bedrock`` control plane) is classified by host
    as ``aws.bedrock`` but runs no model: metadata, not refused (ADR-0319)."""
    capsule, captured, _ = _capture(env, monkeypatch, [{"op": "list_models"}, {"op": "converse"}])
    result, replay, replayed = _replay(env, monkeypatch, capsule)
    assert result.exit_code == 0, result.output
    assert replayed is not None and replayed[1] == captured[1]
    assert replay["model_calls_mocked"] == 1
    assert replay["replay_contract"]["model_calls_not_servable_by_system"] == {}
    # The metadata call ran live (against the fake control plane) and is reported.
    assert [h.split()[2] for h in _hits(env)] == ["/foundation-models"]
    assert replay["replay_contract"]["network_connections_live"] >= 1


# ── LangChain (optional; skipped unless langchain-aws is installed) ──────────


@pytest.mark.parametrize("op", ["converse", "stream"])
def test_langchain_chat_bedrock_converse_with_tools_round_trips(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch, op: str
) -> None:
    pytest.importorskip("langchain_aws")
    plan = [{"client": "langchain", "op": op, "tools": True, "tool_result": "x"}]
    capsule, captured, _ = _capture(env, monkeypatch, plan)
    assert captured[0]["tool_calls"][0]["args"] == {"city": "capture-answer"}
    assert captured[0]["second"]
    result, replay, replayed = _replay(env, monkeypatch, capsule)
    _assert_faithful(env, result, replay, captured, replayed, model_calls=2)
