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
