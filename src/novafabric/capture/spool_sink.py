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
