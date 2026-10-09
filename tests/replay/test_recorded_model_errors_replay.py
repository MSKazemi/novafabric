"""Issue #16 remainder: mocked replay replays RECORDED MODEL ERRORS.

When the captured workload's SDK call raised (rate limit, 4xx, 5xx after the
SDK's own retries, timeout, connection error), mocked replay raises -- inside the
replayed process, at the same position -- the same SDK exception class, built
from what capture recorded (status, message, parsed body, request id, retry
headers). The wire records of the HTTP attempts underneath (ADR-0305 transport
records) are never served, so a call the SDK retried and then completed is
served as the success it was.

End-to-end tests run ``nova capture`` on a real agent process (the real
``openai`` SDK over an ``httpx.MockTransport`` that serves canned HTTP errors;
the stand-in ``anthropic`` package raising its SDK-shaped exceptions), then
``nova replay`` with the network refused. What the agent's ``except`` block saw
during replay must equal what it saw during capture.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Any

import jsonschema
import pytest
import yaml
from _mocked_replay_agent import (
    openai_body,
    openai_chunks,
    openai_record,
    responses_body,
    responses_sse,
    write_agent,
    write_capsule,
    write_fake_anthropic,
)
from typer.testing import CliRunner

from novafabric.capture.hooks._sdk_errors import SDK_ERROR_EXT, describe_sdk_error
from novafabric.cli.main import app
from novafabric.replay import ReplayEngine, ReplayFlags
from novafabric.replay._contract import served_model_records, summarize
from novafabric.replay._model_errors import (
    ALLOWED_SDK_ERRORS,
    UnreconstructableError,
    rebuild_sdk_error,
)

pytest.importorskip("openai")
pytest.importorskip("mcp")

runner = CliRunner()
REPO = Path(__file__).resolve().parents[2]
_SCHEMA = json.loads((REPO / "schemas" / "replay-result.schema.json").read_text())
LEGACY = REPO / "tests" / "fixtures" / "pre-adr0305-double-record" / "model-calls.jsonl"

_RL_BODY = {"error": {"message": "Rate limit reached", "type": "requests",
                      "code": "rate_limit_exceeded", "param": None}}
_RETRY = {"retry-after-ms": "1", "x-request-id": "req_abc"}


def rate_limited() -> dict[str, Any]:
    """One HTTP 429 attempt; the SDK retries it (twice by default)."""
    return {"__status__": 429, "json": _RL_BODY, "headers": _RETRY}


def http_error(status: int, message: str = "bad") -> dict[str, Any]:
    return {"__status__": status,
            "json": {"error": {"message": message, "type": "invalid_request_error",
                               "code": None, "param": None}},
            "headers": {"retry-after-ms": "1", "x-request-id": f"req_{status}"}}


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


def _role(record: dict[str, Any]) -> str | None:
    return (record.get("extensions") or {}).get("io.novafabric.record_role")


def _capture(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch, plan: list[dict[str, Any]],
    *, canned: list[Any] | None = None, anthropic: list[Any] | None = None,
) -> tuple[Path, list[dict[str, Any]]]:
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
    return capsule, json.loads(env["out"].read_text())


def _replay(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch, capsule: Path,
) -> tuple[dict[str, Any], list[dict[str, Any]], int]:
    monkeypatch.delenv("AGENT_CANNED", raising=False)
    monkeypatch.delenv("FAKE_ANTHROPIC_CANNED", raising=False)
    env["out"].unlink(missing_ok=True)
    result = runner.invoke(app, ["replay", str(capsule), "-o", str(env["replays"])])
    (replay_dir,) = list(env["replays"].iterdir())
    replay = yaml.safe_load((replay_dir / "replay_result.yaml").read_text())
    jsonschema.validate(replay, _SCHEMA)
    replayed = json.loads(env["out"].read_text()) if env["out"].exists() else []
    return replay, replayed, result.exit_code


def _round_trip(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch, plan: list[dict[str, Any]],
    **canned: Any,
) -> tuple[Path, list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    capsule, captured = _capture(env, monkeypatch, plan, **canned)
    replay, replayed, code = _replay(env, monkeypatch, capsule)
    assert code == 0, replay
    return capsule, captured, replay, replayed


def _assert_faithful(
    replay: dict[str, Any], captured: list[Any], replayed: list[Any], *,
    model_calls: int, errors: int,
) -> None:
    assert replayed == captured
    assert replay["status"] == "success", replay.get("divergence_reason")
    assert replay["model_calls_mocked"] == replay["model_calls_available"] == model_calls
    assert replay["replay_contract"]["model_errors_replayed"] == errors
    assert replay["replay_contract"]["model_calls_live"] == 0
    assert replay["queues_fully_consumed"] is True
    assert "divergence_reason" not in replay


# ── the headline case: a rate limit is replayed as RateLimitError(429) ───────


def test_rate_limit_is_replayed_as_rate_limit_error_with_status_429(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    capsule, captured, replay, replayed = _round_trip(
        env, monkeypatch, [{"op": "chat", "catch": True}, {"op": "chat"}],
        # three 429 attempts (the SDK's default two retries), then a success
        canned=[rate_limited(), rate_limited(), rate_limited(),
                openai_body(content="after the limit")],
    )
    records = _records(capsule)
    # Capture: 3 transport attempts + 1 logical error, 1 transport + 1 logical success.
    assert [(_role(r), r["status"]) for r in records] == [
        ("transport", "error"), ("transport", "error"), ("transport", "error"),
        ("logical", "error"), ("transport", "success"), ("logical", "success"),
    ]
    detail = records[3]["extensions"][SDK_ERROR_EXT]
    assert detail["class"] == "RateLimitError" and detail["status_code"] == 429
    assert detail["body"] == _RL_BODY["error"]
    assert detail["request_id"] == "req_abc"
    assert detail["response_headers"]["retry-after-ms"] == "1"

    error = replayed[0]
    assert error["error"] == "RateLimitError"
    assert error["status_code"] == error["response_status"] == 429
    assert error["body"] == _RL_BODY["error"] and error["code"] == "rate_limit_exceeded"
    assert error["request_id"] == "req_abc" and error["retry_after_ms"] == "1"
    assert error["response_json"] == _RL_BODY
    assert "APIStatusError" in error["mro"]
    assert replayed[1]["content"] == "after the limit"
    _assert_faithful(replay, captured, replayed, model_calls=2, errors=1)


def test_a_call_retried_twice_then_successful_is_served_as_the_success(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    capsule, captured, replay, replayed = _round_trip(
        env, monkeypatch, [{"op": "chat"}],
        canned=[rate_limited(), http_error(503, "overloaded"), openai_body(content="ok")],
    )
    records = _records(capsule)
    assert [(_role(r), r["status"]) for r in records] == [
        ("transport", "error"), ("transport", "error"), ("transport", "success"),
        ("logical", "success"),
    ]
    assert replayed == [{"op": "chat", "content": "ok", "finish": "stop", "tool_calls": []}]
    _assert_faithful(replay, captured, replayed, model_calls=1, errors=0)


def test_an_error_mid_sequence_then_success(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, captured, replay, replayed = _round_trip(
        env, monkeypatch,
        [{"op": "chat"}, {"op": "chat", "catch": True}, {"op": "chat"}],
        canned=[openai_body(content="one"), http_error(400, "context too long"),
                openai_body(content="three")],
    )
    assert [o.get("content") or o.get("error") for o in replayed] == [
        "one", "BadRequestError", "three"
    ]
    assert replayed[1]["status_code"] == 400
    _assert_faithful(replay, captured, replayed, model_calls=3, errors=1)


# ── every surface ADR-0304 serves ────────────────────────────────────────────


@pytest.mark.parametrize("op, follow_up", [
    ("async_chat", openai_body(content="next")),
    ("stream_chat", openai_chunks(content="next")),
    ("async_stream_chat", openai_chunks(content="next")),
    ("responses", responses_body(text="next")),
    ("async_responses", responses_body(text="next")),
    ("stream_responses", responses_sse(responses_body(text="next"))),
    ("async_stream_responses", responses_sse(responses_body(text="next"))),
])
def test_recorded_errors_replay_on_every_openai_surface(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch, op: str, follow_up: Any
) -> None:
    _, captured, replay, replayed = _round_trip(
        env, monkeypatch, [{"op": op, "catch": True}, {"op": op}],
        canned=[http_error(500, "server exploded")] * 3 + [follow_up],
    )
    assert replayed[0]["error"] == "InternalServerError"
    assert replayed[0]["status_code"] == 500
    assert "error" not in replayed[1]
    _assert_faithful(replay, captured, replayed, model_calls=2, errors=1)


@pytest.mark.parametrize("failure, expected", [
    ({"__raise__": "timeout"}, "APITimeoutError"),
    ({"__raise__": "connect"}, "APIConnectionError"),
])
def test_timeouts_and_connection_errors_are_replayed(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch,
    failure: dict[str, str], expected: str,
) -> None:
    _, captured, replay, replayed = _round_trip(
        env, monkeypatch, [{"op": "chat", "catch": True}],
        canned=[failure] * 3,
    )
    assert replayed[0]["error"] == expected
    assert replayed[0]["request"] == ["POST", "https://api.openai.com/v1/chat/completions"]
    assert "status_code" not in replayed[0]
    _assert_faithful(replay, captured, replayed, model_calls=1, errors=1)


@pytest.mark.parametrize("op", [
    "anthropic", "anthropic_async", "anthropic_stream", "anthropic_async_stream",
])
def test_recorded_anthropic_errors_are_replayed(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch, op: str
) -> None:
    body = {"type": "error", "error": {"type": "rate_limit_error", "message": "slow"}}
    capsule, captured, replay, replayed = _round_trip(
        env, monkeypatch, [{"op": op, "catch": True}],
        anthropic=[{"__error__": {"class": "RateLimitError", "status": 429, "body": body,
                                  "headers": {"request-id": "req_ant", "retry-after": "2"}}}],
    )
    (record,) = _records(capsule)
    assert record["extensions"][SDK_ERROR_EXT]["class"] == "RateLimitError"
    assert replayed[0]["error"] == "RateLimitError"
    assert replayed[0]["status_code"] == 429 and replayed[0]["request_id"] == "req_ant"
    assert replayed[0]["body"] == body
    _assert_faithful(replay, captured, replayed, model_calls=1, errors=1)


# ── fail closed when the error cannot be rebuilt faithfully ──────────────────


def _error_record(detail: dict[str, Any] | None, *, error_type: str = "RateLimitError",
                  call_id: str = "01HXAY7M5JZ8R7K4P9DPBYK2E0") -> dict[str, Any]:
    ext: dict[str, Any] = {"io.novafabric.api_surface": "openai.chat.completions",
                           "io.novafabric.record_role": "logical",
                           "io.novafabric.logical_call_id": call_id}
    if detail is not None:
        ext[SDK_ERROR_EXT] = detail
    return {
        "model_call_id": call_id, "gen_ai.system": "openai",
        "gen_ai.request.model": "gpt-4o", "gen_ai.response.choices": [],
        "status": "error", "extensions": ext,
        "error": {"type": error_type, "message": "recorded failure", "traceback_ref": None},
    }


def _synthetic_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, records: list[dict[str, Any]],
    plan: list[dict[str, Any]], *, permissive: bool = False,
) -> tuple[Any, list[dict[str, Any]]]:
    agent = write_agent(tmp_path)
    out = tmp_path / "out.json"
    monkeypatch.setenv("AGENT_OUT", str(out))
    monkeypatch.setenv("AGENT_SIDE", str(tmp_path / "side.log"))
    monkeypatch.setenv("AGENT_PLAN", json.dumps(plan))
    monkeypatch.delenv("AGENT_CANNED", raising=False)
    cap = write_capsule(tmp_path, agent, records)
    result = ReplayEngine(
        capsule_dir=cap, flags=ReplayFlags(mode="mocked", permissive=permissive),
        base_dir=tmp_path / "replays",
    ).run()
    jsonschema.validate(result.as_dict(), _SCHEMA)
    return result, (json.loads(out.read_text()) if out.exists() else [])


_UNKNOWN = {"sdk": "openai", "class": "OAuthError", "message": "recorded failure",
            "status_code": 401, "body": None}


def test_an_unknown_error_class_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, obs = _synthetic_replay(
        tmp_path, monkeypatch,
        [_error_record(_UNKNOWN, error_type="OAuthError"), openai_record("later")],
        [{"op": "chat", "catch": True}, {"op": "chat", "catch": True}],
    )
    assert obs[0]["error"] == "ReplayRecordedErrorUnreconstructableError"
    assert result.status == "failure"
    assert result.error is not None and result.error["type"] == "ReplayDivergence"
    assert result.replay_contract is not None
    kinds = [d["kind"] for d in result.replay_contract["divergences"]]
    assert kinds[0] == "recorded_error_unreconstructable"
    first = result.replay_contract["divergences"][0]
    assert first["error_type"] == "OAuthError" and "allowed" in first["reason"]
    # The refused record is not consumed, so the next call is refused too --
    # it is never handed the later call's response.
    assert obs[1]["error"] == "ReplayRecordedErrorUnreconstructableError"
    assert result.replay_contract["model_errors_replayed"] == 0
    assert result.model_calls_mocked == 0


@pytest.mark.parametrize("detail, reason", [
    (None, "captured before SDK error details were recorded"),
    ({"sdk": "openai", "class": "RateLimitError", "message": "recorded failure"},
     "no HTTP status was recorded"),
    ({"sdk": "openai", "class": "RateLimitError", "message": "recorded failure",
      "status_code": 429, "body_omitted": "larger than 65536 characters"},
     "the error body was not recorded"),
    ({"sdk": "anthropic", "class": "RateLimitError", "message": "recorded failure",
      "status_code": 429}, "recorded for SDK 'anthropic'"),
])
def test_an_error_that_cannot_be_rebuilt_faithfully_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, detail: Any, reason: str
) -> None:
    result, obs = _synthetic_replay(
        tmp_path, monkeypatch, [_error_record(detail)], [{"op": "chat", "catch": True}],
    )
    assert obs[0]["error"] == "ReplayRecordedErrorUnreconstructableError"
    assert result.status == "failure"
    assert result.replay_contract is not None
    first = result.replay_contract["divergences"][0]
    assert first["kind"] == "recorded_error_unreconstructable"
    assert reason in first["reason"]


def test_permissive_raises_a_stand_in_and_still_reports_the_divergence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, obs = _synthetic_replay(
        tmp_path, monkeypatch,
        [_error_record(_UNKNOWN, error_type="OAuthError"), openai_record("later")],
        [{"op": "chat", "catch": True}, {"op": "chat"}],
        permissive=True,
    )
    assert obs[0]["error"] == "ReplayRecordedModelError"
    assert obs[0]["message"] == "OAuthError: recorded failure"
    assert obs[1]["content"] == "later"
    assert result.status == "success"  # permissive: the status follows the exit code
    assert result.replay_contract is not None
    kinds = [d["kind"] for d in result.replay_contract["divergences"]]
    assert kinds == ["recorded_error_unreconstructable"]
    assert result.replay_contract["model_errors_replayed"] == 0
    assert result.model_calls_mocked == 2


# ── old capsules: a response-less wire record is never served as an error ────


def _legacy_records() -> list[dict[str, Any]]:
    return [json.loads(x) for x in LEGACY.read_text().splitlines() if x.strip()]


def test_legacy_capsule_serves_no_wire_record_as_an_error() -> None:
    records = _legacy_records()
    assert not any("io.novafabric.record_role" in (r.get("extensions") or {})
                   for r in records), "the fixture must be pre-ADR-0305"
    served = served_model_records(records)
    # sync, streamed, retried (its 500 wire attempt is NOT a position), failed.
    assert [(r["gen_ai.request.messages"][0]["content"], r["status"]) for r in served] == [
        ("sync", "success"), ("stream", "success"), ("retry", "success"),
        ("fail", "error"),
    ]
    assert all("error" in r for r in served if r["status"] == "error")


def test_legacy_capsule_replays_until_its_unrebuildable_error_then_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, obs = _synthetic_replay(
        tmp_path, monkeypatch, _legacy_records(),
        [{"op": "chat"}, {"op": "stream_chat"}, {"op": "chat"}, {"op": "chat", "catch": True}],
    )
    # The retried call's 500 wire attempt was not served: call 3 got its success.
    assert [o.get("content") for o in obs[:3]] == ["hello", "hello", "hello"]
    assert obs[3]["error"] == "ReplayRecordedErrorUnreconstructableError"
    assert result.status == "failure"
    assert result.replay_contract is not None
    first = result.replay_contract["divergences"][0]
    assert first["kind"] == "recorded_error_unreconstructable"
    assert first["error_type"] == "BadRequestError"
    assert "re-capture" in first["reason"]


# ── units: allow-list, capture detail, counters ──────────────────────────────


def test_the_allow_list_names_only_classes_the_sdk_defines() -> None:
    import openai

    for name in ALLOWED_SDK_ERRORS["openai"]:
        cls = getattr(openai, name)
        assert issubclass(cls, openai.APIError), name


def test_rebuild_never_imports_a_module_named_by_the_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib

    imported: list[str] = []
    real = importlib.import_module

    def spy(name: str, *args: Any) -> Any:
        imported.append(name)
        return real(name, *args)

    monkeypatch.setattr(importlib, "import_module", spy)
    hostile = {"sdk": "openai", "class": "RateLimitError", "message": "m",
               "status_code": 429, "body": None, "module": "os",
               "response_type": "subprocess"}
    record = _error_record(hostile)
    exc = rebuild_sdk_error(record)
    assert type(exc).__name__ == "RateLimitError"
    assert "os" not in imported and "subprocess" not in imported
    with pytest.raises(UnreconstructableError):
        rebuild_sdk_error({**record, "gen_ai.system": "os"})


def test_describe_sdk_error_keeps_only_retry_and_rate_limit_headers() -> None:
    import httpx
    import openai

    request = httpx.Request("POST", "https://api.example/v1/chat/completions?api-key=SECRET")
    response = httpx.Response(429, json={"error": {"message": "m"}}, request=request,
                              headers={"retry-after": "3", "set-cookie": "s=1",
                                       "x-ratelimit-remaining-requests": "0"})
    exc = openai.RateLimitError("m", response=response, body={"message": "m"})
    detail = describe_sdk_error(exc, "openai")
    assert detail["request_url"] == "https://api.example/v1/chat/completions"
    assert set(detail["response_headers"]) == {
        "retry-after", "x-ratelimit-remaining-requests", "content-type",
    }
    assert detail["status_code"] == 429 and detail["body"] == {"message": "m"}


def test_describe_sdk_error_never_raises_on_a_hostile_exception() -> None:
    class Weird(Exception):
        @property
        def message(self) -> str:
            raise RuntimeError("boom")

    detail = describe_sdk_error(Weird("x"), "openai")
    assert detail["sdk"] == "openai" and detail["class"] == "Weird"


def test_summary_counts_replayed_errors_and_unreconstructable_ones() -> None:
    records = [_error_record(_UNKNOWN), openai_record("ok")]
    events = [
        {"event": "installed"},
        {"event": "model_served", "queue": "openai", "recorded_error": "RateLimitError",
         "pid": 1},
        {"event": "model_served", "queue": "openai", "pid": 1},
    ]
    report = summarize(records, [], events, divergence_policy="fail", substitute_tools=True)
    assert report.model_errors_replayed == 1
    assert report.model_calls_mocked == 2 and report.model_calls_available == 2
    events = [{"event": "installed"},
              {"event": "divergence", "kind": "recorded_error_unreconstructable",
               "message": "m"}]
    report = summarize(records, [], events, divergence_policy="fail", substitute_tools=True)
    assert report.model_calls_unmatched == 1
    assert report.model_errors_replayed == 0


def test_cli_prints_replayed_errors(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    capsule, _ = _capture(
        env, monkeypatch, [{"op": "chat", "catch": True}],
        canned=[http_error(400)],
    )
    monkeypatch.delenv("AGENT_CANNED", raising=False)
    result = runner.invoke(app, ["replay", str(capsule), "-o", str(env["replays"])])
    assert result.exit_code == 0, result.output
    assert "1 of 1 served from the capsule (1 raised as the recorded error)" in result.output
    shutil.rmtree(env["replays"])


# ══ Follow-ups: errors raised MID-STREAM, and Responses API failed/incomplete ══
#
# Acceptance criteria (written before the implementation):
#
# AC1 capture -- a streamed SDK call whose iteration raises an ``Exception``
#     after k chunks is ONE logical record: ``status: error``, the ``error``
#     block, ``extensions["io.novafabric.sdk_error"]`` (as for an error at
#     ``create``), ``nova.streaming.chunk_count == k``, ``stream_complete:
#     false``, and the content delivered before the error. Not ``success``.
# AC2 replay -- mocked replay serves, at that position, a stream that delivers
#     the same content (text, tool calls) with no closing or terminal event,
#     then raises the same SDK exception class (``openai.APIError`` with the
#     error payload for an in-stream error event; ``APIConnectionError`` /
#     ``APITimeoutError`` for a dropped connection). Sync and async, Chat
#     Completions, Responses API, Messages. Counted in ``model_errors_replayed``.
# AC3 fail closed -- an exception that cannot be rebuilt is refused at
#     ``create`` (``recorded_error_unreconstructable``), before any chunk is
#     served; ``--permissive`` serves the chunks, then the stand-in.
# AC4 Responses ``status: failed`` -- the SDK RETURNS it (non-streamed) and
#     YIELDS ``response.failed`` (streamed); it never raises (openai 3.26.1:
#     ``resources/responses/responses.py`` posts with ``cast_to=Response``;
#     ``_streaming.py`` raises only on a payload carrying an ``error`` key).
#     Replay does the same: a ``Response`` with ``status: failed`` and the
#     recorded ``error``, or a ``response.failed`` terminal event. Nothing is
#     raised; it counts as a served response, not as a replayed error.
# AC5 the Responses ``status`` is replayed verbatim -- capture records
#     ``status``, ``incomplete_details`` and ``error`` from the provider's
#     Response (``io.novafabric.response_status``), so an ``incomplete``
#     response whose reason the finish-reason mapping cannot express is
#     replayed as ``incomplete`` with that reason, not as ``completed``.
# AC6 a failed Responses record captured before AC5 is refused with a
#     re-capture reason (never served as ``completed``, never raised).

_CHUNK_BASE = {"id": "chatcmpl-m", "object": "chat.completion.chunk", "created": 1,
               "model": "gpt-4o"}
_STREAM_ERROR = {"error": {"message": "The server had an error while processing your "
                                      "request.", "type": "server_error", "code": None,
                           "param": None}}


def _chunk(delta: dict[str, Any]) -> dict[str, Any]:
    return {**_CHUNK_BASE, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}


def _chat_sse_then_error(*, drop: str | None = None) -> dict[str, Any]:
    """Three content chunks, then the failure: the provider's in-stream error
    payload, or (``drop``) a dropped connection."""
    events: list[dict[str, Any]] = [
        _chunk({"role": "assistant", "content": ""}),
        _chunk({"content": "Hel"}), _chunk({"content": "lo"}),
    ]
    if drop:
        return {"__sse__": events, "drop": drop, "done": False}
    return {"__sse__": [*events, _STREAM_ERROR]}


@pytest.mark.parametrize("op", ["stream_chat", "async_stream_chat"])
def test_an_error_event_mid_stream_is_replayed_after_the_delivered_chunks(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch, op: str
) -> None:
    capsule, captured, replay, replayed = _round_trip(
        env, monkeypatch, [{"op": op, "catch": True}, {"op": "chat"}],
        canned=[_chat_sse_then_error(), openai_body(content="next")],
    )
    record = [r for r in _records(capsule) if _role(r) == "logical"][0]
    # AC1: the stream that raised is recorded as an error, with what it delivered.
    assert record["status"] == "error"
    assert record["error"]["type"] == "APIError"
    detail = record["extensions"][SDK_ERROR_EXT]
    assert detail["class"] == "APIError" and detail["body"] == _STREAM_ERROR["error"]
    assert record["nova.streaming"]["chunk_count"] == 3
    assert record["extensions"]["io.novafabric.stream_complete"] is False
    assert record["gen_ai.response.choices"][0]["message"]["content"] == "Hello"
    # AC2: replay delivers the same content, then raises the same class.
    error = replayed[0]
    assert error["error"] == "APIError" and error["body"] == _STREAM_ERROR["error"]
    assert error["type"] == "server_error"
    assert error["delivered"]["content"] == "Hello"
    assert error["delivered"]["finish"] is None
    assert replayed[1]["content"] == "next"
    _assert_faithful(replay, captured, replayed, model_calls=2, errors=1)


@pytest.mark.parametrize("drop, expected", [
    ("read", "APIConnectionError"), ("timeout", "APITimeoutError"),
])
def test_a_connection_dropped_mid_stream_is_replayed_after_the_delivered_chunks(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch, drop: str, expected: str
) -> None:
    capsule, captured, replay, replayed = _round_trip(
        env, monkeypatch, [{"op": "stream_chat", "catch": True}],
        canned=[_chat_sse_then_error(drop=drop)],
    )
    (record,) = [r for r in _records(capsule) if _role(r) == "logical"]
    assert record["status"] == "error" and record["error"]["type"] == expected
    assert replayed[0]["error"] == expected
    assert replayed[0]["delivered"]["content"] == "Hello"
    _assert_faithful(replay, captured, replayed, model_calls=1, errors=1)


def _responses_sse_dropped(text: str) -> dict[str, Any]:
    """A Responses stream cut after the text deltas: no done or terminal events."""
    full = responses_sse(responses_body(text=text))["__sse__"]
    kept = [e for e in full if not e["type"].endswith((".done", "completed"))]
    return {"__sse__": kept, "named_events": True, "done": False, "drop": "read"}


@pytest.mark.parametrize("op", ["stream_responses", "async_stream_responses"])
def test_a_responses_stream_dropped_mid_way_is_replayed(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch, op: str
) -> None:
    capsule, captured, replay, replayed = _round_trip(
        env, monkeypatch, [{"op": op, "catch": True}],
        canned=[_responses_sse_dropped("partial answer")],
    )
    (record,) = [r for r in _records(capsule) if _role(r) == "logical"]
    assert record["status"] == "error"
    assert "io.novafabric.response_status" not in record["extensions"]
    assert replayed[0]["error"] == "APIConnectionError"
    assert replayed[0]["delivered"] == {
        "types": ["response.created", "response.output_item.added",
                  "response.content_part.added", "response.output_text.delta"],
        "text": "partial answer",
    }
    _assert_faithful(replay, captured, replayed, model_calls=1, errors=1)


@pytest.mark.parametrize("op", ["anthropic_stream", "anthropic_async_stream"])
def test_an_anthropic_error_mid_stream_is_replayed(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch, op: str
) -> None:
    from _mocked_replay_agent import anthropic_events

    events = anthropic_events("Hello there", tool=("toolu_1", "get_weather", {"city": "Rome"}))
    body = {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}
    # Cut after the text block closed and the tool block opened, before its input.
    cut = events["events"][:8]
    assert [e["type"] for e in cut[-2:]] == ["content_block_stop", "content_block_start"]
    cut.append({"__error__": {"class": "OverloadedError", "status": 529, "body": body}})
    capsule, captured, replay, replayed = _round_trip(
        env, monkeypatch, [{"op": op, "catch": True}], anthropic=[{"events": cut}],
    )
    (record,) = _records(capsule)
    assert record["status"] == "error" and record["nova.streaming"]["chunk_count"] == 8
    assert replayed[0]["error"] == "OverloadedError" and replayed[0]["body"] == body
    assert replayed[0]["delivered"]["stop_reason"] is None
    assert [b["type"] for b in replayed[0]["delivered"]["blocks"]] == ["text", "tool_use"]
    _assert_faithful(replay, captured, replayed, model_calls=1, errors=1)


def _stream_error_record(detail: dict[str, Any]) -> dict[str, Any]:
    record = _error_record(detail, error_type=str(detail.get("class")))
    record["gen_ai.response.choices"] = [{
        "index": 0, "message": {"role": "assistant", "content": "Hel"},
        "finish_reason": "stop"}]
    record["nova.streaming"] = {"streamed": True, "chunk_count": 2, "first_token_ms": 1}
    record["extensions"]["io.novafabric.stream_complete"] = False
    return record


_VALUE_ERROR = {"sdk": "openai", "class": "ValueError", "message": "recorded failure"}


def test_a_mid_stream_error_that_cannot_be_rebuilt_is_refused_before_any_chunk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, obs = _synthetic_replay(
        tmp_path, monkeypatch, [_stream_error_record(_VALUE_ERROR)],
        [{"op": "stream_chat", "catch": True}],
    )
    assert obs[0]["error"] == "ReplayRecordedErrorUnreconstructableError"
    assert "delivered" not in obs[0]
    assert result.replay_contract is not None
    assert result.replay_contract["divergences"][0]["kind"] == (
        "recorded_error_unreconstructable"
    )


def test_permissive_serves_the_delivered_chunks_then_the_stand_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, obs = _synthetic_replay(
        tmp_path, monkeypatch, [_stream_error_record(_VALUE_ERROR)],
        [{"op": "stream_chat", "catch": True}], permissive=True,
    )
    assert obs[0]["error"] == "ReplayRecordedModelError"
    assert obs[0]["delivered"]["content"] == "Hel"
    assert obs[0]["delivered"]["finish"] is None


# ── Responses API: status failed / incomplete are returned, not raised ───────

_FAILED_ERROR = {"code": "server_error", "message": "The model failed to generate."}


def _failed_body(*, text: str | None = None) -> dict[str, Any]:
    return {**responses_body(text=text, rid="resp_failed"), "status": "failed",
            "error": _FAILED_ERROR, "usage": None}


@pytest.mark.parametrize("op, canned", [
    ("responses", _failed_body()),
    ("async_responses", _failed_body()),
    ("stream_responses", responses_sse(_failed_body(text="half"))),
    ("async_stream_responses", responses_sse(_failed_body(text="half"))),
])
def test_a_failed_responses_response_is_returned_not_raised(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch, op: str, canned: Any
) -> None:
    capsule, captured, replay, replayed = _round_trip(
        env, monkeypatch, [{"op": op}, {"op": "chat"}],
        canned=[canned, openai_body(content="next")],
    )
    record = [r for r in _records(capsule) if _role(r) == "logical"][0]
    assert record["status"] == "error"
    assert record["extensions"]["io.novafabric.response_status"] == {
        "status": "failed", "incomplete_details": None, "error": _FAILED_ERROR,
    }
    assert SDK_ERROR_EXT not in record["extensions"]
    # The SDK returned it: the workload read status and error, nothing raised.
    assert replayed[0]["status"] == "failed" and replayed[0]["response_error"] == _FAILED_ERROR
    _assert_faithful(replay, captured, replayed, model_calls=2, errors=0)


@pytest.mark.parametrize("op", ["responses", "stream_responses"])
def test_an_incomplete_responses_response_keeps_its_recorded_reason(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch, op: str
) -> None:
    # "max_messages" is a reason the installed SDK defines that the finish-reason
    # mapping cannot express; before AC5 it was replayed as "completed".
    body = {**responses_body(text="cut"), "status": "incomplete",
            "incomplete_details": {"reason": "max_messages"}}
    canned = body if op == "responses" else responses_sse(body)
    _, captured, replay, replayed = _round_trip(
        env, monkeypatch, [{"op": op}], canned=[canned],
    )
    assert replayed[0]["status"] == "incomplete"
    assert replayed[0]["incomplete"] == "max_messages"
    _assert_faithful(replay, captured, replayed, model_calls=1, errors=0)


# ── golden fixtures (tests/fixtures/recorded-model-errors/) ──────────────────

GOLDEN = REPO / "tests" / "fixtures" / "recorded-model-errors"


def _golden(name: str) -> list[dict[str, Any]]:
    path = GOLDEN / name
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


#: The in-force model-call schema (``schemas/`` holds the not-yet-in-force v1 target).
_MODEL_CALL_SCHEMA = REPO / "src" / "novafabric" / "schemas" / "model-call.schema.json"


def test_golden_records_are_valid_and_every_extension_they_use_is_described() -> None:
    schema = json.loads(_MODEL_CALL_SCHEMA.read_text())
    described = set(schema["properties"]["extensions"]["properties"])
    for name in ("failed-response.jsonl", "mid-stream-error.jsonl",
                 "legacy-failed-response.jsonl", "legacy-mid-stream-error.jsonl",
                 "responses-error-event.jsonl"):
        for record in _golden(name):
            jsonschema.validate(record, schema)
            assert set(record["extensions"]) <= described, name


def test_a_response_status_without_its_status_is_invalid() -> None:
    schema = json.loads(_MODEL_CALL_SCHEMA.read_text())
    (record,) = _golden("failed-response.jsonl")
    record["extensions"]["io.novafabric.response_status"].pop("status")
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(record, schema)


def test_golden_failed_response_replays_as_returned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, obs = _synthetic_replay(
        tmp_path, monkeypatch, _golden("failed-response.jsonl"), [{"op": "responses"}],
    )
    assert obs[0]["status"] == "failed" and obs[0]["response_error"] == _FAILED_ERROR
    assert result.status == "success"


@pytest.mark.parametrize("name", ["mid-stream-error.jsonl", "legacy-mid-stream-error.jsonl"])
def test_golden_mid_stream_error_replays_the_chunks_then_the_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    # The legacy record (captured before a missing finish reason was recorded as
    # null) holds a fabricated "stop"; neither serves a finish reason.
    (record,) = _golden(name)
    expected = None if name.startswith("mid") else "stop"
    assert record["gen_ai.response.choices"][0]["finish_reason"] == expected
    result, obs = _synthetic_replay(
        tmp_path, monkeypatch, [record], [{"op": "stream_chat", "catch": True}],
    )
    assert obs[0]["error"] == "APIError"
    assert obs[0]["delivered"]["content"] == "Hello"
    assert obs[0]["delivered"]["finish"] is None
    assert result.status == "success"
    assert result.replay_contract is not None
    assert result.replay_contract["model_errors_replayed"] == 1


def test_golden_responses_error_event_replays_the_events_then_the_error_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, obs = _synthetic_replay(
        tmp_path, monkeypatch, _golden("responses-error-event.jsonl"),
        [{"op": "stream_responses_events"}],
    )
    assert obs[0]["types"] == ["response.created", "response.output_item.added",
                               "response.content_part.added",
                               "response.output_text.delta", "error"]
    assert obs[0]["text"] == "partial"
    assert obs[0]["errors"] == [{"code": "server_error", "message": _STREAM_ERROR["error"][
        "message"], "param": None, "sequence_number": 4}]
    assert result.status == "success"
    assert result.replay_contract is not None
    assert result.replay_contract["model_errors_replayed"] == 0


def test_a_failed_response_captured_before_its_status_was_recorded_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, obs = _synthetic_replay(
        tmp_path, monkeypatch, _golden("legacy-failed-response.jsonl"),
        [{"op": "responses", "catch": True}],
    )
    assert obs[0]["error"] == "ReplayRecordedErrorUnreconstructableError"
    assert result.replay_contract is not None
    first = result.replay_contract["divergences"][0]
    assert first["kind"] == "recorded_error_unreconstructable"
    assert "status 'failed'" in first["reason"] and "re-capture" in first["reason"]
