"""NovaFabric adapter for AWS Bedrock AgentCore.

Wraps a ``boto3`` ``bedrock-agent-runtime`` client. ``invoke_agent()`` is
intercepted and its ``completion`` EventStream is passed through unchanged while
each AgentCore trace part (every ``Trace`` kind: orchestration, pre-/post-
processing, routing, custom orchestration, guardrail, failure) is appended to
``bedrock-traces.jsonl`` — the body at the ``forensic`` capture level, a digest
otherwise. The capsule is written on the shared adapter core and closes when the
stream ends, however it ends (``success`` / ``failure`` / ``partial``).

``boto3`` is an **optional** dependency.  :func:`wrap_client` raises
:class:`ImportError` at call time if not installed.
"""
from __future__ import annotations

import contextvars
import hashlib
import json
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from novafabric.adapters._capsule import AdapterCapture, begin_capture
from novafabric.adapters._streaming import (
    Closer,
    close_when_collected,
    guard_iterator,
    record_outcome,
)
from novafabric.capture._jsonable import json_default
from novafabric.capture.record import _payloads_enabled

_FRAMEWORK = "bedrock-agentcore"
_TRACES_FILE = "bedrock-traces.jsonl"
#: ``Trace`` members in the ``bedrock-agent-runtime`` service model (2023-07-26).
_TRACE_KINDS = (
    "orchestrationTrace",
    "preProcessingTrace",
    "postProcessingTrace",
    "routingClassifierTrace",
    "customOrchestrationTrace",
    "guardrailTrace",
    "failureTrace",
)

#: Set while this adapter is producing a stream event, so a nested
#: ``invoke_agent`` records into the running capsule instead of opening a second.
_in_flight: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "novafabric_bedrock_in_flight", default=False
)


def _digest(value: Any) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), default=json_default)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def trace_records(event: Any, *, payloads: bool) -> list[dict[str, Any]]:
    """The ``bedrock-traces.jsonl`` records for one ``ResponseStream`` event.

    botocore yields ``{"trace": TracePart}``, and a ``TracePart`` carries the
    trace kinds under its own ``trace`` member. Each kind present becomes one
    record: identifiers and the event time always; the trace body itself only
    when the capture level keeps payloads (``forensic``/``air_gapped``,
    ADR-0066 Amendment 1), otherwise its digest — an orchestration trace holds
    the full model prompt.
    """
    part = event.get("trace") if isinstance(event, Mapping) else None
    if not isinstance(part, Mapping):
        return []
    inner = part.get("trace")
    if not isinstance(inner, Mapping):
        return []
    records: list[dict[str, Any]] = []
    for kind in _TRACE_KINDS:
        if kind not in inner:
            continue
        record: dict[str, Any] = {
            "trace_type": kind,
            "agent_id": part.get("agentId"),
            "agent_alias_id": part.get("agentAliasId"),
            "agent_version": part.get("agentVersion"),
            "collaborator_name": part.get("collaboratorName"),
            "event_time": part.get("eventTime"),
        }
        if payloads:
            record[kind] = inner[kind]
        else:
            record["digest"] = _digest(inner[kind])
        records.append(record)
    return records


def _recorded(stream: Iterator[Any], cap: AdapterCapture) -> Iterator[Any]:
    """Yield *stream* unchanged; append its trace parts to the capsule."""
    payloads = _payloads_enabled()
    for event in stream:
        for record in trace_records(event, payloads=payloads):
            cap.writer.append_jsonl(_TRACES_FILE, record)
            cap.extensions.setdefault("com.amazonaws.bedrock", {"traces_ref": _TRACES_FILE})
        yield event


class _WrappedBedrockClient:
    """Thin wrapper around a boto3 bedrock-agent-runtime client."""

    def __init__(self, inner: Any, data_dir: Path) -> None:
        self._inner = inner
        self._data_dir = data_dir

    def invoke_agent(self, **kwargs: Any) -> dict[str, Any]:
        """``invoke_agent`` with the run captured until its completion stream ends.

        The capsule closes where the run really ends: the stream exhausted
        (``success``), raising (``failure``, the error still propagates), or
        closed, abandoned or garbage-collected unread (``partial``). The input
        text is never copied into the manifest.
        """
        if _in_flight.get():
            nested: dict[str, Any] = self._inner.invoke_agent(**kwargs)
            return nested
        agent_id = str(kwargs.get("agentId", "agent"))
        cap = begin_capture(framework=_FRAMEWORK, run_name=agent_id, data_dir=self._data_dir)
        if not isinstance(cap, AdapterCapture):  # an unusable capsule store: run uncaptured
            uncaptured: dict[str, Any] = self._inner.invoke_agent(**kwargs)
            return uncaptured
        cap.tags["agent_id"] = str(kwargs.get("agentId", ""))
        cap.tags["agent_alias_id"] = str(kwargs.get("agentAliasId", ""))
        try:
            response: dict[str, Any] = self._inner.invoke_agent(**kwargs)
        except BaseException as exc:
            record_outcome(cap, exc)
            cap.finish()
            raise
        closer = Closer(cap)
        stream = response.get("completion")
        guarded = guard_iterator(
            _recorded(iter(stream if stream is not None else ()), cap), closer, _in_flight
        )
        close_when_collected(guarded, closer)
        return {**response, "completion": guarded}

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def wrap_client(client: Any, data_dir: Path | None = None) -> _WrappedBedrockClient:
    """Wrap a boto3 ``bedrock-agent-runtime`` client for NovaFabric capture.

    Args:
        client: A ``boto3.client('bedrock-agent-runtime')`` instance.
        data_dir: Base directory for capsules.

    Returns:
        Wrapped client with the same interface.

    Raises:
        ImportError: If ``boto3`` is not installed.

    Usage::

        import boto3
        from novafabric.adapters.bedrock_agentcore import wrap_client
        client = wrap_client(boto3.client("bedrock-agent-runtime", region_name="us-east-1"))
        response = client.invoke_agent(agentId="...", agentAliasId="...",
                                       sessionId="...", inputText="hello")
        for event in response["completion"]:
            if "chunk" in event:
                print(event["chunk"]["bytes"].decode())
    """
    try:
        import boto3  # noqa: F401
    except ImportError:
        raise ImportError(
            "boto3 is not installed. "
            "Install it with: pip install 'novafabric[bedrock-agentcore]'"
        )

    from novafabric._paths import adapter_default_runs_dir

    resolved = data_dir or adapter_default_runs_dir()
    return _WrappedBedrockClient(client, resolved)
