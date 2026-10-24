"""The Bedrock AgentCore adapter (review eval-adapters-query-12).

(a) ``bedrock-traces.jsonl`` was never written: the adapter looked for
``orchestrationTrace`` in ``event["trace"]``, but botocore's ``ResponseStream.trace``
is a ``TracePart`` whose trace kinds live one level down, at
``event["trace"]["trace"]`` (``bedrock-agent-runtime`` service model, shapes
``TracePart`` / ``Trace``) — while the manifest pointed at the file.
(b) The caller's whole ``inputText`` was copied into ``capsule.yaml`` at every
capture level.
(c) A caller that stopped reading the completion stream, or a stream that raised
mid-way (throttling), left no capsule at all and the wire hooks installed for the
rest of the process; every capsule that was written said ``status: success``.
"""
from __future__ import annotations

import datetime as dt
import gc
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml

from novafabric.adapters.bedrock_agentcore import _WrappedBedrockClient

_PROMPT = "my secret plan for Q4"


def _trace_event(kind: str, body: dict[str, Any]) -> dict[str, Any]:
    """One ``ResponseStream`` event carrying a ``TracePart``, as botocore yields it."""
    return {
        "trace": {
            "agentId": "AGENT1",
            "agentAliasId": "ALIAS1",
            "sessionId": "sess-1",
            "eventTime": dt.datetime(2026, 10, 24, tzinfo=dt.timezone.utc),
            "trace": {kind: body},
        }
    }


_ORCH = {"modelInvocationInput": {"text": f"Human: {_PROMPT}", "type": "ORCHESTRATION"}}


def _events() -> list[dict[str, Any]]:
    return [
        _trace_event("orchestrationTrace", _ORCH),
        _trace_event("guardrailTrace", {"action": "NONE"}),
        {"chunk": {"bytes": b"Hello"}},
    ]


@pytest.fixture(autouse=True)
def _no_real_hooks() -> Iterator[None]:
    with (
        patch("novafabric.adapters._capsule.capture_environment", return_value={}),
        patch("novafabric.capture.hooks.install_all"),
        patch("novafabric.capture.hooks.uninstall_all"),
    ):
        yield


def _invoke(tmp_path: Path, stream: Any) -> dict[str, Any]:
    inner = MagicMock()
    inner.invoke_agent.return_value = {"completion": stream, "sessionId": "sess-1"}
    client = _WrappedBedrockClient(inner, tmp_path)
    return client.invoke_agent(
        agentId="AGENT1", agentAliasId="ALIAS1", sessionId="sess-1", inputText=_PROMPT
    )


def _capsule(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    (manifest_path,) = tmp_path.rglob("capsule.yaml")
    return manifest_path.parent, yaml.safe_load(manifest_path.read_text())


def _traces(cap_dir: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in (cap_dir / "bedrock-traces.jsonl").read_text().splitlines()]


# ── (a) the trace stream is written from the shape botocore yields ────────────


def test_trace_parts_are_written_and_the_manifest_points_at_them(tmp_path: Path) -> None:
    response = _invoke(tmp_path, iter(_events()))
    assert len(list(response["completion"])) == 3  # the caller sees every event
    cap_dir, manifest = _capsule(tmp_path)
    records = _traces(cap_dir)
    assert [r["trace_type"] for r in records] == ["orchestrationTrace", "guardrailTrace"]
    assert records[0]["agent_id"] == "AGENT1"
    assert manifest["extensions"]["com.amazonaws.bedrock"]["traces_ref"] == "bedrock-traces.jsonl"
    assert manifest["status"] == "success"


def test_no_trace_events_means_no_traces_ref(tmp_path: Path) -> None:
    """enableTrace is off by default: then there is no file and no pointer to one."""
    list(_invoke(tmp_path, iter([{"chunk": {"bytes": b"Hi"}}]))["completion"])
    cap_dir, manifest = _capsule(tmp_path)
    assert not (cap_dir / "bedrock-traces.jsonl").exists()
    assert "com.amazonaws.bedrock" not in (manifest.get("extensions") or {})


# ── (b) the prompt is not copied into the manifest; traces follow the level ──


def test_the_prompt_is_not_in_the_manifest(tmp_path: Path) -> None:
    list(_invoke(tmp_path, iter(_events()))["completion"])
    cap_dir, manifest = _capsule(tmp_path)
    assert manifest["inputs"] == []
    assert _PROMPT not in (cap_dir / "capsule.yaml").read_text()


def test_at_the_standard_level_a_trace_is_kept_as_a_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOVA_CAPTURE_LEVEL", "standard")
    list(_invoke(tmp_path, iter(_events()))["completion"])
    cap_dir, _ = _capsule(tmp_path)
    (orch, _guard) = _traces(cap_dir)
    assert "orchestrationTrace" not in orch
    assert orch["digest"].startswith("sha256:")
    assert _PROMPT not in (cap_dir / "bedrock-traces.jsonl").read_text()


def test_at_the_forensic_level_the_trace_payload_is_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOVA_CAPTURE_LEVEL", "forensic")
    list(_invoke(tmp_path, iter(_events()))["completion"])
    cap_dir, _ = _capsule(tmp_path)
    (orch, _guard) = _traces(cap_dir)
    assert orch["orchestrationTrace"] == _ORCH


# ── (c) every way the stream can end leaves a truthful capsule ──────────────


def test_an_abandoned_stream_is_closed_as_partial(tmp_path: Path) -> None:
    completion = _invoke(tmp_path, iter(_events()))["completion"]
    next(completion)
    completion.close()  # the caller `break`s out of the loop
    _, manifest = _capsule(tmp_path)
    assert manifest["status"] == "partial"
    assert manifest["metadata"]["partial_reason"] == "abandoned"


def test_a_stream_never_read_is_closed_when_dropped(tmp_path: Path) -> None:
    response = _invoke(tmp_path, iter(_events()))
    del response
    gc.collect()
    _, manifest = _capsule(tmp_path)
    assert manifest["status"] == "partial"


def test_a_stream_that_raises_mid_way_is_a_failure(tmp_path: Path) -> None:
    class Throttled(Exception):
        pass

    def stream() -> Iterator[dict[str, Any]]:
        yield _events()[0]
        raise Throttled("throttlingException: rate exceeded")

    completion = _invoke(tmp_path, stream())["completion"]
    with pytest.raises(Throttled):
        list(completion)
    _, manifest = _capsule(tmp_path)
    assert (manifest["status"], manifest["exit_code"]) == ("failure", 1)
    assert manifest["error"]["type"] == "Throttled"


def test_a_failed_invoke_call_is_captured_as_a_failure(tmp_path: Path) -> None:
    inner = MagicMock()
    inner.invoke_agent.side_effect = RuntimeError("AccessDenied")
    with pytest.raises(RuntimeError):
        _WrappedBedrockClient(inner, tmp_path).invoke_agent(agentId="AGENT1")
    _, manifest = _capsule(tmp_path)
    assert manifest["status"] == "failure"
