"""Capture and mocked replay never fabricate how a stream ended (ADR-0304 follow-on).

Acceptance criteria:

AC1  A streamed call whose stream never delivered a finish reason (abandoned by
     the workload, failed part-way, or ended without one) is recorded with
     ``finish_reason: null`` on that choice and no ``gen_ai.response.finish_reasons``
     -- never ``"stop"``. Chat Completions, Responses API and Messages alike.
AC2  ``model-call.schema.json`` (all three copies) admits ``null`` for
     ``Choice.finish_reason`` and nothing else new; such records validate.
AC3  Mocked replay serves no finish reason the record does not hold: no chat
     finish-reason or usage chunk, no Responses done/terminal events for an
     unfinished item, no Anthropic ``message_delta`` / ``message_stop``; a
     non-streamed response carries ``None``. A record with a finish reason -- a
     legacy record with ``"stop"`` included -- is served exactly as before.
AC4  A Responses API stream that yields an ``error`` event (the SDK yields it, it
     does not raise: ``openai/_streaming.py`` raises only for a payload with a
     top-level ``error`` key) and then ends is recorded as ``status: error`` with
     the event verbatim under ``io.novafabric.stream_error_event``.
AC5  Replay serves its delivered events, then that error event, then ends -- no
     ``response.completed``; nothing is raised, and it is not counted as a
     replayed error.
AC6  What the workload saw during replay equals what it saw during capture.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import jsonschema
import pytest
import yaml
from _mocked_replay_agent import (
    anthropic_events,
    openai_chunks,
    responses_body,
    responses_sse,
    write_agent,
    write_fake_anthropic,
)
from typer.testing import CliRunner

from novafabric.cli.main import app
from novafabric.replay._dispatcher import build_served_response

pytest.importorskip("openai")
pytest.importorskip("mcp")

runner = CliRunner()
REPO = Path(__file__).resolve().parents[2]
_RESULT_SCHEMA = json.loads((REPO / "schemas" / "replay-result.schema.json").read_text())
_SCHEMA_COPIES = (
    REPO / "schemas" / "model-call.schema.json",
    REPO / "src" / "novafabric" / "schemas" / "model-call.schema.json",
    REPO / "ui" / "dashboard" / "src" / "data" / "schemas" / "model-call.schema.json",
)
ERROR_EVENT_EXT = "io.novafabric.stream_error_event"
STREAM_COMPLETE_EXT = "io.novafabric.stream_complete"


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    paths = {
        "agent": write_agent(tmp_path),
        "out": tmp_path / "out.json",
        "runs": tmp_path / "runs",
        "replays": tmp_path / "replays",
    }
    monkeypatch.setenv("AGENT_OUT", str(paths["out"]))
    monkeypatch.setenv("AGENT_SIDE", str(tmp_path / "side.log"))
    for var in ("FORCE_COLOR", "COLORTERM", "AGENT_CANNED", "FAKE_ANTHROPIC_CANNED"):
        monkeypatch.delenv(var, raising=False)
    return paths


def _logical(capsule: Path) -> list[dict[str, Any]]:
    lines = (capsule / "model-calls.jsonl").read_text().splitlines()
    records = [json.loads(line) for line in lines if line.strip()]
    return [r for r in records
            if (r.get("extensions") or {}).get("io.novafabric.record_role") != "transport"]


def _round_trip(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch, plan: list[dict[str, Any]],
    *, canned: list[Any] | None = None, anthropic: list[Any] | None = None,
) -> tuple[list[dict[str, Any]], list[Any], dict[str, Any], list[Any]]:
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
    monkeypatch.delenv("AGENT_CANNED", raising=False)
    monkeypatch.delenv("FAKE_ANTHROPIC_CANNED", raising=False)
    env["out"].unlink()
    result = runner.invoke(app, ["replay", str(capsule), "-o", str(env["replays"])])
    (replay_dir,) = list(env["replays"].iterdir())
    replay = yaml.safe_load((replay_dir / "replay_result.yaml").read_text())
    jsonschema.validate(replay, _RESULT_SCHEMA)
    assert result.exit_code == 0, replay
    return _logical(capsule), captured, replay, json.loads(env["out"].read_text())


def _assert_faithful(replay: dict[str, Any], captured: Any, replayed: Any, calls: int) -> None:
    assert replayed == captured
    assert replay["status"] == "success", replay.get("divergence_reason")
    assert replay["model_calls_mocked"] == calls
    assert replay["replay_contract"]["model_errors_replayed"] == 0
    assert replay["queues_fully_consumed"] is True


def _validate(record: dict[str, Any]) -> None:
    """Against the in-force schema (``schemas/`` holds the not-yet-in-force v1 target)."""
    jsonschema.validate(record, json.loads(_SCHEMA_COPIES[1].read_text()))


def _chat_without_finish(content: str) -> dict[str, Any]:
    """A chat stream that ends ([DONE]) without ever sending a finish reason."""
    sse = openai_chunks(content=content)
    return {"__sse__": [e for e in sse["__sse__"] if not e["choices"][0]["finish_reason"]]}


# ── AC1 + AC3 + AC6: no finish reason delivered, none recorded, none served ──


@pytest.mark.parametrize("op", ["stream_chat", "async_stream_chat"])
def test_a_chat_stream_that_never_finished_is_recorded_and_replayed_without_one(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch, op: str
) -> None:
    (record,), captured, replay, replayed = _round_trip(
        env, monkeypatch, [{"op": op}], canned=[_chat_without_finish("Hello there")],
    )
    assert captured[0]["finish"] is None and captured[0]["content"] == "Hello there"
    assert record["gen_ai.response.choices"][0]["finish_reason"] is None
    assert "gen_ai.response.finish_reasons" not in record
    assert "io.novafabric.provider_finish_reasons" not in record["extensions"]
    _validate(record)
    _assert_faithful(replay, captured, replayed, calls=1)


def test_a_chat_stream_that_finished_keeps_its_finish_reason(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    (record,), captured, replay, replayed = _round_trip(
        env, monkeypatch, [{"op": "stream_chat", "usage": True}],
        canned=[openai_chunks(content="Hello", usage=True)],
    )
    assert record["gen_ai.response.choices"][0]["finish_reason"] == "stop"
    assert record["gen_ai.response.finish_reasons"] == ["stop"]
    assert replayed[0]["finish"] == "stop" and replayed[0]["usage"] == 8
    _assert_faithful(replay, captured, replayed, calls=1)


def test_an_abandoned_chat_stream_records_no_finish_reason(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    (record,), captured, replay, replayed = _round_trip(
        env, monkeypatch, [{"op": "stream_chat_partial"}],
        canned=[openai_chunks(content="abcdefghi", split=3)],
    )
    assert record["extensions"][STREAM_COMPLETE_EXT] is False
    assert record["gen_ai.response.choices"][0]["finish_reason"] is None
    assert "gen_ai.response.finish_reasons" not in record
    _validate(record)
    _assert_faithful(replay, captured, replayed, calls=1)


@pytest.mark.parametrize("op", ["anthropic_stream", "anthropic_async_stream"])
def test_an_anthropic_stream_without_a_stop_reason_is_recorded_and_replayed_without_one(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch, op: str
) -> None:
    events = anthropic_events("Hello there")["events"]
    cut = [e for e in events if e["type"] not in ("message_delta", "message_stop")]
    (record,), captured, replay, replayed = _round_trip(
        env, monkeypatch, [{"op": op}], anthropic=[{"events": cut}],
    )
    assert captured[0]["stop_reason"] is None
    assert record["gen_ai.response.choices"][0]["finish_reason"] is None
    assert "gen_ai.response.finish_reasons" not in record
    assert "io.novafabric.provider_finish_reasons" not in record["extensions"]
    _validate(record)
    _assert_faithful(replay, captured, replayed, calls=1)


# ── AC4 + AC5 + AC6: a Responses stream that delivered an `error` event ──────

_ERROR_EVENT = {"type": "error", "code": "server_error",
                "message": "The server had an error while processing your request.",
                "param": None}


def _responses_then_error_event(text: str) -> dict[str, Any]:
    """Created, the text deltas, then an ``error`` event and a normal end."""
    sse = responses_sse(responses_body(text=text))
    kept = [e for e in sse["__sse__"] if not e["type"].endswith((".done", "completed"))]
    error = {**_ERROR_EVENT, "sequence_number": len(kept)}
    return {**sse, "__sse__": [*kept, error]}


@pytest.mark.parametrize("op", ["stream_responses_events", "async_stream_responses_events"])
def test_a_responses_error_event_is_recorded_and_replayed_as_delivered(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch, op: str
) -> None:
    stream = _responses_then_error_event("partial answer")
    seq = stream["__sse__"][-1]["sequence_number"]
    records, captured, replay, replayed = _round_trip(
        env, monkeypatch, [{"op": op}, {"op": "chat"}],
        canned=[stream,
                {"id": "c", "object": "chat.completion", "created": 1, "model": "gpt-4o",
                 "choices": [{"index": 0, "finish_reason": "stop",
                              "message": {"role": "assistant", "content": "next"}}]}],
    )
    record = records[0]
    # The SDK yielded the error event; the workload saw it and the stream ended.
    assert captured[0]["types"][-1] == "error"
    assert captured[0]["errors"] == [{**{k: v for k, v in _ERROR_EVENT.items()
                                         if k != "type"}, "sequence_number": seq}]
    # AC4: recorded as a failed call, with the event verbatim, nothing invented.
    assert record["status"] == "error"
    assert record["error"]["type"] == "server_error"
    assert record["error"]["message"] == _ERROR_EVENT["message"]
    assert record["extensions"][ERROR_EVENT_EXT] == {**_ERROR_EVENT, "sequence_number": seq}
    assert "io.novafabric.sdk_error" not in record["extensions"]
    assert "io.novafabric.response_status" not in record["extensions"]
    assert STREAM_COMPLETE_EXT not in record["extensions"]  # the stream ran to its end
    assert record["gen_ai.response.choices"][0]["finish_reason"] is None
    assert "gen_ai.response.finish_reasons" not in record
    _validate(record)
    # AC5 + AC6: the same events, the error event last, no response.completed.
    assert "response.completed" not in replayed[0]["types"]
    _assert_faithful(replay, captured, replayed, calls=2)


# ── AC3 at the builder: what a record without a finish reason is served as ───


def _record(surface: str, finish: str | None, **extra: Any) -> dict[str, Any]:
    return {
        "model_call_id": "01HXAY7M5JZ8R7K4P9DPBYK2M0", "gen_ai.system": "openai",
        "gen_ai.response.id": "rid-1", "gen_ai.response.model": "m-1",
        "gen_ai.usage.input_tokens": 3, "gen_ai.usage.output_tokens": 5,
        "gen_ai.response.choices": [{"index": 0, "finish_reason": finish,
                                     "message": {"role": "assistant", "content": "hi"}}],
        "status": "success", "extensions": {"io.novafabric.api_surface": surface},
        **extra,
    }


def _types(items: Any) -> list[str]:
    return [str(getattr(i, "type", "")) for i in items]


def test_a_chat_record_without_a_finish_reason_is_served_without_one() -> None:
    record = _record("openai.chat.completions", None)
    chunks = list(build_served_response(
        "openai", record, stream=True, asynchronous=False,
        kwargs={"stream_options": {"include_usage": True}}))
    assert [c.choices[0].finish_reason for c in chunks if c.choices] == [None]
    assert not [c for c in chunks if not c.choices]  # no usage chunk either
    plain = build_served_response("openai", record, stream=False, asynchronous=False)
    assert plain.choices[0].finish_reason is None


def test_a_legacy_stop_record_is_served_exactly_as_before() -> None:
    record = _record("openai.chat.completions", "stop")
    chunks = list(build_served_response(
        "openai", record, stream=True, asynchronous=False,
        kwargs={"stream_options": {"include_usage": True}}))
    assert [c.choices[0].finish_reason for c in chunks if c.choices] == [None, "stop"]
    assert chunks[-1].usage.total_tokens == 8


def test_a_responses_record_without_a_finish_reason_gets_no_terminal_event() -> None:
    events = _types(build_served_response(
        "openai.responses", _record("openai.responses", None), stream=True,
        asynchronous=False))
    assert events == ["response.created", "response.output_item.added",
                      "response.content_part.added", "response.output_text.delta"]


def test_an_anthropic_record_without_a_finish_reason_gets_no_message_delta() -> None:
    record = _record("anthropic.messages", None)
    record["gen_ai.system"] = "anthropic"
    events = _types(build_served_response("anthropic", record, stream=True,
                                          asynchronous=False))
    assert "message_delta" not in events and "message_stop" not in events
    plain = build_served_response("anthropic", record, stream=False, asynchronous=False)
    assert plain.stop_reason is None


def test_a_recorded_error_event_is_served_last_and_verbatim() -> None:
    record = _record("openai.responses", None, status="error",
                     error={"type": "server_error", "message": "m", "traceback_ref": None})
    record["extensions"][ERROR_EVENT_EXT] = {**_ERROR_EVENT, "sequence_number": 9}
    record["nova.streaming"] = {"streamed": True, "chunk_count": 5, "first_token_ms": 1}
    items = list(build_served_response("openai.responses", record, stream=True,
                                       asynchronous=False))
    assert _types(items)[-1] == "error"
    assert "response.completed" not in _types(items)
    last = items[-1]
    assert (last.code, last.message, last.param, last.sequence_number) == (
        "server_error", _ERROR_EVENT["message"], None, 9)


def test_an_error_event_omitted_for_size_is_refused_not_served() -> None:
    from novafabric.replay._model_errors import (
        UnreconstructableError,
        is_returned_failed_response,
        rebuild_sdk_error,
    )

    record = _record("openai.responses", None, status="error",
                     error={"type": "error", "message": "m", "traceback_ref": None})
    record["extensions"][ERROR_EVENT_EXT] = {"type": "error", "omitted": "too large"}
    record["nova.streaming"] = {"streamed": True, "chunk_count": 5, "first_token_ms": 1}
    assert not is_returned_failed_response(record)
    with pytest.raises(UnreconstructableError, match="error event"):
        rebuild_sdk_error(record)


# ── AC2: the schema admits null, and only null, beside the enum ──────────────


@pytest.mark.parametrize("path", _SCHEMA_COPIES, ids=lambda p: str(p.relative_to(REPO)))
def test_every_schema_copy_admits_a_null_finish_reason_and_describes_the_error_event(
    path: Path,
) -> None:
    schema = json.loads(path.read_text())
    validator = jsonschema.Draft202012Validator(
        {"$defs": schema["$defs"], "$ref": "#/$defs/Choice"})
    base = {"index": 0, "message": {"role": "assistant", "content": "x"}}
    assert validator.is_valid({**base, "finish_reason": None})
    assert validator.is_valid({**base, "finish_reason": "stop"})
    assert not validator.is_valid({**base, "finish_reason": "end_turn"})
    assert not validator.is_valid(base)  # still required: null is explicit
    assert ERROR_EVENT_EXT in schema["properties"]["extensions"]["properties"]
