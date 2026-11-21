"""ADR-0324: generic wire-level record and serve for registry model hosts.

Each test captures a real agent process that calls in-process fake providers over raw
``httpx`` / ``requests`` (``tests/_wire_agent.py``), then replays the capsule while the
fake server answers something else. A faithful replay observes exactly what the capture
observed, and the server receives no request during it. No provider-specific code is
involved: the providers are only entries in a test URL registry.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from _wire_agent import write_agent, write_registry
from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()

WIRE_RESPONSE = "io.novafabric.wire.response"
WIRE_REQUEST = "io.novafabric.wire.request"
SURFACE = "io.novafabric.api_surface"


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    paths = {
        "agent": write_agent(tmp_path),
        "out": tmp_path / "out.json",
        "side": tmp_path / "side.log",
        "runs": tmp_path / "runs",
        "replays": tmp_path / "replays",
    }
    monkeypatch.setenv("NOVAFABRIC_URL_REGISTRY", str(write_registry(tmp_path)))
    monkeypatch.setenv("AGENT_OUT", str(paths["out"]))
    monkeypatch.setenv("AGENT_SIDE", str(paths["side"]))
    for var in ("FORCE_COLOR", "COLORTERM", "OLLAMA_BASE_URL", "OLLAMA_HOST",
                "NOVAFABRIC_CAPTURE_WIRE_RESPONSES", "HTTP_PROXY", "HTTPS_PROXY",
                "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    return paths


def _records(capsule: Path) -> list[dict[str, Any]]:
    path = capsule / "model-calls.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _capture(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch, plan: list[dict[str, Any]],
) -> tuple[Path, list[dict[str, Any]], list[str]]:
    monkeypatch.setenv("AGENT_PLAN", json.dumps(plan))
    monkeypatch.setenv("WIRE_CANNED", "capture-answer")
    result = runner.invoke(
        app, ["capture", "--output-dir", str(env["runs"]), sys.executable, str(env["agent"])]
    )
    assert result.exit_code == 0, result.output
    (capsule,) = [p for p in env["runs"].iterdir() if (p / "capsule.yaml").exists()]
    captured = json.loads(env["out"].read_text())
    hits = env["side"].read_text().splitlines()
    env["out"].unlink()
    env["side"].unlink()
    return capsule, captured, hits


def _replay(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch, capsule: Path,
    *, plan: list[dict[str, Any]] | None = None, args: tuple[str, ...] = (),
) -> tuple[Any, dict[str, Any], list[dict[str, Any]] | None]:
    if plan is not None:
        monkeypatch.setenv("AGENT_PLAN", json.dumps(plan))
    monkeypatch.setenv("WIRE_CANNED", "REPLAY-LIVE-ANSWER")
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
    assert not env["side"].exists(), (
        "the fake provider was hit during mocked replay: " + env["side"].read_text()
    )
    assert replay["status"] == "success"
    assert replay["model_calls_mocked"] == replay["model_calls_available"] == model_calls
    assert replay["queues_fully_consumed"] is True
    contract = replay["replay_contract"]
    assert contract["model_calls_live"] == 0
    assert contract["network_connections_live"] == 0
    assert "divergence_reason" not in replay


@pytest.mark.parametrize("client", ["httpx", "httpx-async", "requests"])
def test_a_mistral_chat_completion_round_trips(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch, client: str
) -> None:
    plan = [{"api": "mistral", "client": client, "prompt": "hi"},
            {"api": "mistral", "client": client, "prompt": "again"}]
    capsule, captured, hits = _capture(env, monkeypatch, plan)
    assert [c["text"] for c in captured] == [
        "capture-answer number 1", "capture-answer number 2"
    ]
    assert len(hits) == 2

    records = _records(capsule)
    assert len(records) == 2
    for record in records:
        assert record["gen_ai.system"] == "mistral"
        ext = record["extensions"]
        assert ext[SURFACE] == "generic-wire"
        assert ext["io.novafabric.record_role"] == "logical"
        response = ext[WIRE_RESPONSE]
        assert response["status"] == 200
        assert response["streamed"] is False and response["complete"] is True
        assert json.loads("".join(response["chunks"]))["object"] == "chat.completion"
        headers = dict(response["headers"])
        assert headers["content-type"] == "application/json"
        assert "content-length" not in headers
        assert ext[WIRE_REQUEST]["method"] == "POST"
        assert ext[WIRE_REQUEST]["path"] == "/mistral/v1/chat/completions"
        assert ext[WIRE_REQUEST]["digest"].startswith("sha256:")
        # Credentials never reach the record through this mechanism.
        assert "tok-1" not in json.dumps(ext)

    result, replay, replayed = _replay(env, monkeypatch, capsule)
    _assert_faithful(env, result, replay, captured, replayed, model_calls=2)
    surfaces = " ".join(replay["replay_contract"]["interception_surfaces"])
    assert "generic wire (registry model hosts)" in surfaces


@pytest.mark.parametrize("client", ["httpx", "httpx-async", "requests"])
def test_a_streamed_sse_response_is_replayed_with_identical_chunks(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch, client: str
) -> None:
    plan = [{"api": "mistral", "client": client, "stream": True, "prompt": "stream please"}]
    capsule, captured, _ = _capture(env, monkeypatch, plan)
    assert captured[0]["text"] == "capture-answer number 1"
    assert captured[0]["events"] == 5  # four deltas + [DONE]

    (record,) = _records(capsule)
    response = record["extensions"][WIRE_RESPONSE]
    assert response["streamed"] is True and response["complete"] is True
    assert dict(response["headers"])["content-type"] == "text/event-stream"
    body = "".join(response["chunks"])
    assert body.endswith("data: [DONE]\n\n")
    if "chunks" in captured[0]:  # httpx: the workload saw the raw chunks
        assert response["chunks"] == captured[0]["chunks"]

    result, replay, replayed = _replay(env, monkeypatch, capsule)
    _assert_faithful(env, result, replay, captured, replayed, model_calls=1)


def test_gemini_generate_content_and_an_openai_compatible_local_server_are_served(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = [
        {"api": "gemini", "client": "httpx", "prompt": "g1"},
        {"api": "gemini", "client": "requests", "stream": True, "prompt": "g2"},
        {"api": "local", "client": "httpx-async", "prompt": "l1"},
        {"api": "local", "client": "requests", "prompt": "l2"},
    ]
    capsule, captured, hits = _capture(env, monkeypatch, plan)
    assert len(hits) == 4
    assert captured[0]["text"] == "capture-answer number 1"
    assert captured[1]["text"] == "capture-answernumber2"  # Gemini parts, joined
    records = _records(capsule)
    assert [r["gen_ai.system"] for r in records] == [
        "gemini", "gemini", "local.openai-compatible", "local.openai-compatible"
    ]
    assert all(r["extensions"][SURFACE] == "generic-wire" for r in records)
    # Gemini's API key travels in the query; it is not part of the recorded match key.
    assert records[1]["extensions"][WIRE_REQUEST]["path"].endswith(":streamGenerateContent")

    result, replay, replayed = _replay(env, monkeypatch, capsule)
    _assert_faithful(env, result, replay, captured, replayed, model_calls=4)


def test_a_changed_request_body_at_replay_is_a_divergence_never_live(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    capsule, _, _ = _capture(env, monkeypatch, [{"api": "mistral", "prompt": "hi"}])
    result, replay, replayed = _replay(
        env, monkeypatch, capsule,
        plan=[{"api": "mistral", "prompt": "a different question", "catch": True}],
    )
    assert result.exit_code != 0
    assert replay["status"] == "failure"
    assert not env["side"].exists(), "a mismatched request reached the provider"
    divergences = replay["replay_contract"]["divergences"]
    # The mismatch, and the recorded call that was therefore never served.
    assert [d["kind"] for d in divergences] == ["wire_request_mismatch", "model_calls_unconsumed"]
    divergence = divergences[0]
    assert divergence["system"] == "mistral"
    assert divergence["path"] == "/mistral/v1/chat/completions"
    assert "a different question" not in json.dumps(divergence)
    assert replayed is not None and replayed[0]["error"]  # the workload saw the refusal


def test_a_changed_request_body_under_permissive_runs_live_and_is_counted(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    capsule, _, _ = _capture(env, monkeypatch, [{"api": "mistral", "prompt": "hi"}])
    result, replay, replayed = _replay(
        env, monkeypatch, capsule, plan=[{"api": "mistral", "prompt": "other"}],
        args=("--permissive",),
    )
    assert replayed == [{"status": 200, "text": "REPLAY-LIVE-ANSWER number 1"}]
    assert env["side"].exists()
    contract = replay["replay_contract"]
    assert contract["model_calls_live"] == 1
    assert [d["kind"] for d in contract["divergences"]] == [
        "wire_request_mismatch", "model_calls_unconsumed"
    ]


def test_changed_volatile_headers_and_credentials_still_match(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = [{"api": "mistral", "client": "requests", "prompt": "hi"},
            {"api": "gemini", "client": "httpx", "prompt": "hi"}]
    monkeypatch.setenv("WIRE_TOKEN", "capture-token")
    monkeypatch.setenv("WIRE_REQUEST_ID", "capture-request-id")
    capsule, captured, _ = _capture(env, monkeypatch, plan)
    monkeypatch.setenv("WIRE_TOKEN", "replay-token")
    monkeypatch.setenv("WIRE_REQUEST_ID", "replay-request-id")
    result, replay, replayed = _replay(env, monkeypatch, capsule)
    _assert_faithful(env, result, replay, captured, replayed, model_calls=2)


def test_a_recorded_http_error_and_its_retry_are_served_as_recorded(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = [{"api": "mistral", "client": "requests", "prompt": "hi", "fail_first": 1},
            {"api": "local", "client": "httpx", "prompt": "hi", "fail_first": 2}]
    capsule, captured, hits = _capture(env, monkeypatch, plan)
    assert len(hits) == 5
    assert [c["status"] for c in captured] == [200, 200]
    records = _records(capsule)
    statuses = [r["extensions"][WIRE_RESPONSE]["status"] for r in records]
    assert statuses == [429, 200, 429, 429, 200]
    throttle = records[0]["extensions"][WIRE_RESPONSE]
    headers = dict(throttle["headers"])
    assert headers["retry-after"] == "0"
    assert "set-cookie" not in headers  # never recorded, so never replayed
    assert records[0]["status"] == "error"

    result, replay, replayed = _replay(env, monkeypatch, capsule)
    _assert_faithful(env, result, replay, captured, replayed, model_calls=5)


def test_recording_off_stores_no_body_and_strict_replay_refuses_as_before(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOVAFABRIC_CAPTURE_WIRE_RESPONSES", "off")
    capsule, _, _ = _capture(env, monkeypatch, [{"api": "mistral", "prompt": "hi"}])
    monkeypatch.delenv("NOVAFABRIC_CAPTURE_WIRE_RESPONSES")
    (record,) = _records(capsule)
    ext = record["extensions"]
    assert WIRE_RESPONSE not in ext
    assert "NOVAFABRIC_CAPTURE_WIRE_RESPONSES" in ext["io.novafabric.response_not_recorded"]
    assert "capture-answer" not in json.dumps(record)

    result, replay, replayed = _replay(env, monkeypatch, capsule)
    assert result.exit_code == 1
    assert replayed is None  # refused before the workload ran
    assert not env["side"].exists()
    assert replay["error"]["type"] == "ModelSurfaceNotServable"
    assert replay["model_calls_not_servable"] == 1
