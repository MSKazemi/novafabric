"""ADR-0320 (issue #51): Ollama ``/api/chat`` and ``/api/generate`` calls are
recorded with their response at capture and served by mocked replay.

Each test captures a real agent process (raw ``httpx`` to an in-process fake Ollama
server, probe P7's shape), then replays the capsule while the fake server answers
something else. The replay must observe exactly what the capture observed, and the
server must receive no request during the replay.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from _ollama_agent import write_agent
from typer.testing import CliRunner

from novafabric.cli.main import app

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
    for var in ("FORCE_COLOR", "COLORTERM", "OLLAMA_BASE_URL", "OLLAMA_HOST"):
        monkeypatch.delenv(var, raising=False)
    return paths


def _records(capsule: Path) -> list[dict[str, Any]]:
    path = capsule / "model-calls.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _capture(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch, plan: list[dict[str, Any]]
) -> tuple[Path, list[dict[str, Any]]]:
    monkeypatch.setenv("AGENT_PLAN", json.dumps(plan))
    monkeypatch.setenv("OLLAMA_CANNED", "capture-answer")
    result = runner.invoke(
        app, ["capture", "--output-dir", str(env["runs"]), sys.executable, str(env["agent"])]
    )
    assert result.exit_code == 0, result.output
    (capsule,) = [p for p in env["runs"].iterdir() if (p / "capsule.yaml").exists()]
    captured = json.loads(env["out"].read_text())
    hits = env["side"].read_text().splitlines()
    assert len(hits) == len(plan), hits
    env["out"].unlink()
    env["side"].unlink()
    return capsule, captured


def _replay(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch, capsule: Path,
    *, plan: list[dict[str, Any]] | None = None, args: tuple[str, ...] = (),
) -> tuple[Any, dict[str, Any], list[dict[str, Any]] | None]:
    if plan is not None:
        monkeypatch.setenv("AGENT_PLAN", json.dumps(plan))
    monkeypatch.setenv("OLLAMA_CANNED", "REPLAY-LIVE-ANSWER")
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
        "the fake Ollama server was hit during mocked replay: "
        + env["side"].read_text()
    )
    assert replay["status"] == "success"
    assert replay["model_calls_mocked"] == replay["model_calls_available"] == model_calls
    assert replay["queues_fully_consumed"] is True
    contract = replay["replay_contract"]
    assert contract["model_calls_live"] == 0
    assert contract["network_connections_live"] == 0
    assert "divergence_reason" not in replay


PLAN_ALL = [
    {"op": "chat", "stream": False},
    {"op": "chat", "stream": True},
    {"op": "generate", "stream": False},
    {"op": "generate", "stream": True},
]


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
def test_ollama_chat_and_generate_round_trip_streamed_and_not(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch, asynchronous: bool
) -> None:
    plan = [{**step, "async": asynchronous} for step in PLAN_ALL]
    capsule, captured = _capture(env, monkeypatch, plan)
    assert [c["content"] for c in captured] == [f"capture-answer #{n}" for n in (1, 2, 3, 4)]

    records = _records(capsule)
    assert len(records) == 4
    surfaces = [r["extensions"]["io.novafabric.api_surface"] for r in records]
    assert surfaces == ["ollama.chat", "ollama.chat", "ollama.generate", "ollama.generate"]
    for n, record in enumerate(records, start=1):
        assert record["gen_ai.system"] == "ollama"
        assert record["extensions"]["io.novafabric.record_role"] == "logical"
        (choice,) = record["gen_ai.response.choices"]
        assert choice["message"] == {"role": "assistant", "content": f"capture-answer #{n}"}
        assert choice["finish_reason"] == "stop"
        assert record["gen_ai.usage.input_tokens"] == 7
        assert record["gen_ai.usage.output_tokens"] == 3 + n
        assert record["gen_ai.response.model"] == "fake-model"
        assert record["gen_ai.request.temperature"] == 0.0
        assert record["gen_ai.request.seed"] == 1
    assert records[0]["gen_ai.operation.name"] == "chat"
    assert records[2]["gen_ai.operation.name"] == "text_completion"
    assert records[2]["gen_ai.request.messages"] == [{"role": "user", "content": "hi"}]
    assert "nova.streaming" not in records[0]
    assert records[1]["nova.streaming"]["chunk_count"] == 3

    result, replay, replayed = _replay(env, monkeypatch, capsule)
    _assert_faithful(env, result, replay, captured, replayed, model_calls=4)


def test_a_tool_calling_ollama_response_round_trips(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = [
        {"op": "chat", "stream": False, "tools": True},
        {"op": "chat", "stream": True, "tools": True, "async": True},
    ]
    capsule, captured = _capture(env, monkeypatch, plan)
    expected_call = {"function": {"name": "get_weather", "arguments": {"city": "capture-answer"}}}
    assert [c["tool_calls"] for c in captured] == [[expected_call], [expected_call]]
    for record in _records(capsule):
        (choice,) = record["gen_ai.response.choices"]
        assert choice["message"]["tool_calls"] == [
            {"id": "", "name": "get_weather", "arguments": {"city": "capture-answer"}}
        ]
        assert choice["finish_reason"] == "tool_calls"
        assert record["extensions"]["io.novafabric.provider_finish_reasons"] == ["stop"]

    result, replay, replayed = _replay(env, monkeypatch, capsule)
    _assert_faithful(env, result, replay, captured, replayed, model_calls=2)


def test_an_abandoned_stream_is_recorded_as_such_and_replayed_as_delivered(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = [{"op": "chat", "stream": True, "abandon": True}, {"op": "chat", "stream": False}]
    capsule, captured = _capture(env, monkeypatch, plan)
    # The workload read one line and closed the response.
    assert captured[0] == {"content": "capture-", "tool_calls": [], "done_reason": None,
                           "eval_count": None, "done": False}
    first = _records(capsule)[0]
    # Flagged: the workload did not read the body to its end. The record holds what
    # the transport delivered (here, the whole body) and the chunk shapes.
    assert first["extensions"]["io.novafabric.stream_complete"] is False
    assert first["extensions"]["io.novafabric.ollama.chunks"][0] == [8, 0, 0]

    result, replay, replayed = _replay(env, monkeypatch, capsule)
    # Served in the recorded chunks: the replayed workload reads the same first line.
    assert result.exit_code == 0, result.output
    assert replayed == captured
    assert not env["side"].exists()
    assert replay["model_calls_mocked"] == 2


def test_a_stream_dropped_before_done_is_recorded_without_a_finish_reason() -> None:
    """Unit level: a body that ends without Ollama's ``done`` object."""
    from novafabric.capture.hooks._ollama import OllamaAccumulator, apply_response
    from novafabric.replay._ollama_serve import ollama_response_objects

    acc = OllamaAccumulator("ollama.chat")
    acc.add({"model": "m", "message": {"role": "assistant", "content": "par"}, "done": False})
    acc.add({"model": "m", "message": {"role": "assistant", "content": "tial"}, "done": False})
    record: dict[str, Any] = {"gen_ai.request.model": "m"}
    apply_response(record, acc, streamed=True, chunk_count=2, first_ms=1, complete=False)
    (choice,) = record["gen_ai.response.choices"]
    assert choice == {"index": 0, "message": {"role": "assistant", "content": "partial"},
                      "finish_reason": None}
    assert "gen_ai.response.finish_reasons" not in record
    assert record["extensions"]["io.novafabric.stream_complete"] is False
    served = ollama_response_objects("ollama.chat", record, stream=True)
    assert [o["message"]["content"] for o in served] == ["par", "tial"]
    assert all(o["done"] is False for o in served)


def test_ollama_queue_exhaustion_fails_closed(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    capsule, _ = _capture(env, monkeypatch, [{"op": "chat", "stream": False}])
    plan = [{"op": "chat", "stream": False}, {"op": "chat", "stream": False, "catch": True}]
    result, replay, replayed = _replay(env, monkeypatch, capsule, plan=plan)
    assert replayed is not None
    assert replayed[0]["content"] == "capture-answer #1"
    assert replayed[1]["error"] == "ReplayQueueExhaustedError"
    assert not env["side"].exists(), "an exhausted Ollama queue went to the live server"
    assert replay["status"] == "failure"
    assert "model_queue_exhausted" in replay["divergence_reason"]


def test_a_surface_mismatch_fails_closed(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    capsule, _ = _capture(env, monkeypatch, [{"op": "chat", "stream": False}])
    plan = [{"op": "generate", "stream": False, "catch": True}]
    result, replay, replayed = _replay(env, monkeypatch, capsule, plan=plan)
    assert replayed == [{"error": "ReplayProviderMismatchError", "message": replayed[0]["message"]}]
    assert not env["side"].exists()
    assert replay["status"] == "failure"
    assert "provider_mismatch" in replay["divergence_reason"]


def test_a_record_without_a_response_is_never_served_empty(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A capsule captured before ADR-0320 (``choices: []``, no surface marker):
    the record is not served -- the call is not answered from the capsule."""
    capsule, _ = _capture(env, monkeypatch, [{"op": "chat", "stream": False}])
    path = capsule / "model-calls.jsonl"
    (record,) = _records(capsule)
    record["gen_ai.response.choices"] = []
    del record["extensions"]["io.novafabric.api_surface"]
    record["extensions"].pop("io.novafabric.ollama.final", None)
    path.write_text(json.dumps(record) + "\n")
    result, replay, replayed = _replay(env, monkeypatch, capsule)
    assert replay["model_calls_available"] == 0
    assert replay["model_calls_mocked"] == 0
    # Not served from the capsule (never as an empty answer): either replay refused
    # it up front (#49's not-servable check) or the call reached the live server.
    if replayed is not None:
        assert replayed[0]["content"] == "REPLAY-LIVE-ANSWER #1"
        assert env["side"].read_text().splitlines() == ["hit /api/chat"]


# ── real SDK paths (test extras; skipped when not installed) ─────────────────
# Defaults checked here: the ``ollama`` SDK sends ``"stream": false`` unless
# ``stream=True``; ``langchain_ollama`` always streams.


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
def test_the_ollama_sdk_round_trips(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch, asynchronous: bool
) -> None:
    pytest.importorskip("ollama")
    plan: list[dict[str, Any]] = [
        {"client": "ollama", "op": "chat", "stream": False},
        {"client": "ollama", "op": "chat", "stream": True},
        {"client": "ollama", "op": "generate", "stream": False},
        {"client": "ollama", "op": "generate", "stream": True},
        {"client": "ollama", "op": "chat", "stream": False, "tools": True},
    ]
    plan = [{**step, "async": asynchronous} for step in plan]
    capsule, captured = _capture(env, monkeypatch, plan)
    records = _records(capsule)
    assert ["nova.streaming" in r for r in records] == [False, True, False, True, False]
    assert records[4]["gen_ai.response.choices"][0]["message"]["tool_calls"][0]["name"] == (
        "get_weather"
    )
    result, replay, replayed = _replay(env, monkeypatch, capsule)
    _assert_faithful(env, result, replay, captured, replayed, model_calls=5)


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
def test_langchain_ollama_round_trips(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch, asynchronous: bool
) -> None:
    pytest.importorskip("langchain_ollama")
    plan = [
        {"client": "langchain", "op": "chat", "async": asynchronous},
        {"client": "langchain", "op": "chat", "tools": True, "async": asynchronous},
        {"client": "langchain", "op": "generate", "async": asynchronous},
    ]
    capsule, captured = _capture(env, monkeypatch, plan)
    assert captured[1]["tool_calls"] == [
        {"name": "get_weather", "args": {"city": "capture-answer"}}
    ]
    records = _records(capsule)
    # langchain_ollama streams every call, so each record is folded from NDJSON.
    assert all(r["nova.streaming"]["streamed"] for r in records), json.dumps(records)[:500]
    result, replay, replayed = _replay(env, monkeypatch, capsule)
    _assert_faithful(env, result, replay, captured, replayed, model_calls=3)
