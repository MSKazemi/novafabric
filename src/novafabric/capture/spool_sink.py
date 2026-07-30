"""Capture→spool write integration (ADR-0092 slice C / C0).

``SpoolSink`` converts a captured event into a schema-valid EventEnvelope v1
record and writes it to a NovaFabric spool — the Go-backed durable queue that a
resident drain forwards to the collector tier. It is the edge-side write half of
slice C; the drain/forward half is the ``novafabric-spool-forwarder`` Go binary.

Design contract (mirrors the rest of the capture path):
- **Fail-open.** A spool write failure must never surface to the agent workflow.
- **Edge stays keyless.** No signing happens here; envelopes are forwarded
  unsigned and signed at the hub (ADR-0092 §slice C, OQ-C-1 hub-sign default).
- Local-mode capture is unaffected: the sink is only constructed when spool
  emission is explicitly enabled.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Union

from novafabric.capture._ulid import new_span_id, new_ulid
from novafabric.collector_cffi.spool import NovaPySpool

_ENVELOPE_VERSION = "1"
_SCHEMA_VERSION = "event-envelope/1"


def _new_trace_id() -> str:
    """Return a 32-hex-char W3C trace id."""
    return os.urandom(16).hex()


def _utc_now() -> str:
    """Return an RFC 3339 / ISO-8601 UTC timestamp (schema ``date-time``)."""
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())


class SpoolSink:
    """Writes captured events to a NovaFabric spool as EventEnvelope v1 records."""

    def __init__(self, spool_dir: Union[str, Path]) -> None:
        self._spool = NovaPySpool(spool_dir)

    def emit_event(
        self,
        *,
        event_type: str,
        run_id: str,
        agent_id: str,
        global_run_id: str | None = None,
        parent_run_id: str | None = None,
        cluster_id: str | None = None,
        tenant_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Build a schema-valid EventEnvelope v1 and write it to the spool.

        Fail-open: any error is swallowed so the agent workflow is never blocked.
        """
        try:
            envelope = {
                "envelope_version": _ENVELOPE_VERSION,
                "schema_version": _SCHEMA_VERSION,
                "event_id": new_ulid(),
                "global_run_id": global_run_id or run_id,
                "run_id": run_id,
                "parent_run_id": parent_run_id,
                "event_type": event_type,
                "trace_id": _new_trace_id(),
                "span_id": new_span_id(),
                "agent_id": agent_id,
                "cluster_id": cluster_id,
                "tenant_id": tenant_id,
                "started_at": _utc_now(),
                "payload": payload,
                "payload_hash": None,
            }
            self._spool.write(json.dumps(envelope, separators=(",", ":")).encode())
        except Exception:
            pass  # fail-open: never block the workload

    def close(self) -> None:
        """Release the underlying spool."""
        self._spool.close()


def emit_call_events_from_capsule(
    sink: SpoolSink,
    capsule_dir: Union[str, Path],
    *,
    run_id: str,
    agent_id: str,
    global_run_id: str | None = None,
) -> None:
    """Re-emit each locally-captured model/tool call as a spool event (ADR-0220).

    Reads the already-written ``model-calls.jsonl``/``tool-calls.jsonl`` (OTel
    GenAI semconv records, one line per completed or failed call — there is no
    separate "started" event in the source data, so only the Completed/Failed
    canonical types are emitted, never Started) and re-emits each as a
    ``ModelCallCompleted``/``ModelCallFailed``/``ToolCallCompleted``/
    ``ToolCallFailed`` spool event so ``nova kg ingest --source nats`` has real
    data to build CALLS/USES_TOOL edges from.

    Deliberately excludes request/response message content and tool
    arguments/results from the payload — this re-emission crosses a network
    boundary (NATS), unlike the local capsule file it reads from, so it stays
    to summary fields only (ADR-0021 §4 capture-privacy default). A record
    missing the field the KG pipeline keys edges on (``model_id``/
    ``tool_name``) is skipped, as is a malformed JSON line — both silently,
    matching ``CapsuleEventConsumer``'s own local-dir read tolerance.

    Fail-open throughout: each ``sink.emit_event()`` call already swallows its
    own errors; a missing/unreadable source file is also swallowed rather than
    raised, since this is best-effort re-emission of already-captured evidence.
    """
    capsule_dir = Path(capsule_dir)

    try:
        for line in (capsule_dir / "model-calls.jsonl").read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            model_id = record.get("gen_ai.response.model") or record.get("gen_ai.request.model")
            if not model_id:
                continue
            event_type = (
                "ModelCallCompleted" if record.get("status") == "success" else "ModelCallFailed"
            )
            sink.emit_event(
                event_type=event_type,
                run_id=run_id,
                agent_id=agent_id,
                global_run_id=global_run_id,
                payload={
                    "model_id": model_id,
                    "provider": record.get("gen_ai.system"),
                    "status": record.get("status"),
                    "duration_ms": record.get("duration_ms"),
                    "input_tokens": record.get("gen_ai.usage.input_tokens"),
                    "output_tokens": record.get("gen_ai.usage.output_tokens"),
                },
            )
    except OSError:
        pass  # fail-open: model-calls.jsonl missing/unreadable is not fatal

    try:
        for line in (capsule_dir / "tool-calls.jsonl").read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            tool_name = record.get("tool_name")
            if not tool_name:
                continue
            event_type = (
                "ToolCallCompleted" if record.get("status") == "success" else "ToolCallFailed"
            )
            sink.emit_event(
                event_type=event_type,
                run_id=run_id,
                agent_id=agent_id,
                global_run_id=global_run_id,
                payload={
                    "tool_name": tool_name,
                    "status": record.get("status"),
                    "duration_ms": record.get("duration_ms"),
                    "mutates": record.get("mutates"),
                },
            )
    except OSError:
        pass  # fail-open: tool-calls.jsonl missing/unreadable is not fatal
