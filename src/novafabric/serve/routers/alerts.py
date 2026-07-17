"""Recent operational alerts route group (ADR-0192 D6 read surface, ADR-0183 pattern).

`GET /api/alerts/recent` merges the hash-chained delivery audit log
(`novafabric.audit`, `alert.delivery` entries carry endpoint_id/outcome/
attempts) with recent `ops.*` events from the durable `events.jsonl`
(emitted-but-maybe-undelivered ⇒ outcome ``emitted``), newest-first and capped
at ``limit``. Bounded read — no capsule scans — and fail-safe: a missing audit
log or events file yields an empty result, never a 500.

Built by a factory so the caller injects its own auth dependency
(ADR-0183 §3): ``serve`` passes its shared-token ``verify_token`` closure;
``server`` can mount the same route behind OIDC/RBAC.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query

logger = logging.getLogger(__name__)

# Audit `details["outcome"]` (slice-1 writes "delivered"/"error") → the public
# outcome enum. Anything already public passes straight through.
_OUTCOME_MAP = {"error": "failed"}

_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


def _parse_dt(value: str) -> datetime:
    """Best-effort ISO8601 → aware datetime for sorting; epoch on failure."""
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return _EPOCH


def _delivery_rows(audit_path: Path) -> tuple[list[dict[str, Any]], set[str]]:
    """One row per `alert.delivery` audit entry; also the set of covered event ids."""
    from novafabric.audit import AuditEventType, AuditLog  # noqa: PLC0415

    rows: list[dict[str, Any]] = []
    covered: set[str] = set()
    if not audit_path.exists():
        return rows, covered
    for entry in AuditLog(audit_path).query():
        if entry.event_type != AuditEventType.ALERT_DELIVERY:
            continue
        details = entry.details or {}
        event_id = str(details.get("event_id") or entry.resource_id)
        covered.add(event_id)
        outcome = str(details.get("outcome", ""))
        rows.append({
            "id": entry.entry_id,
            "timestamp": entry.timestamp.isoformat(),
            "event_type": str(details.get("event_type", "")),
            "severity": details.get("severity"),
            "subject": str(details.get("subject") or event_id),
            "outcome": _OUTCOME_MAP.get(outcome, outcome),
            "endpoint_id": details.get("endpoint_id"),
            "attempts": int(details.get("attempts", 0) or 0),
        })
    return rows, covered


def _emitted_rows(events_path: Path, covered: set[str]) -> list[dict[str, Any]]:
    """One `emitted` row per `ops.*` event in the log without a delivery entry."""
    rows: list[dict[str, Any]] = []
    if not events_path.exists():
        return rows
    with events_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            event_type = str(rec.get("type", ""))
            if not event_type.startswith("ops."):
                continue
            event_id = str(rec.get("event_id", ""))
            if event_id in covered:
                continue  # already surfaced as a delivery row
            subject = rec.get("subject") or {}
            rows.append({
                "id": event_id,
                "timestamp": str(rec.get("occurred_at", "")),
                "event_type": event_type,
                "severity": rec.get("severity"),
                "subject": str(subject.get("ref", "")) if isinstance(subject, dict) else "",
                "outcome": "emitted",
                "endpoint_id": None,
                "attempts": 0,
            })
    return rows


def build_alerts_router(
    verify_token: Callable[..., Any],
    *,
    db_path: Path | None,
) -> APIRouter:
    router = APIRouter(dependencies=[Depends(verify_token)], tags=["alerts"])

    @router.get("/api/alerts/recent")
    async def recent_alerts(
        limit: int = Query(default=50, ge=1, le=200),
    ) -> dict[str, Any]:
        from novafabric.audit import AUDIT_LOG_PATH  # noqa: PLC0415
        from novafabric.events.alerts import load_alerts_config_from_env  # noqa: PLC0415
        from novafabric.events.emitter import load_config_from_env  # noqa: PLC0415

        alerts_cfg = load_alerts_config_from_env()
        events_cfg = load_config_from_env()
        audit_path = alerts_cfg.audit_log_path or AUDIT_LOG_PATH

        rows: list[dict[str, Any]] = []
        covered: set[str] = set()
        try:
            rows, covered = _delivery_rows(audit_path)
        except Exception as exc:  # noqa: BLE001 — a read must never 500
            logger.warning("reading alert audit log failed: %s", exc)
        if events_cfg.log_path is not None:
            try:
                rows.extend(_emitted_rows(events_cfg.log_path, covered))
            except Exception as exc:  # noqa: BLE001 — a read must never 500
                logger.warning("reading events log failed: %s", exc)

        rows.sort(key=lambda r: _parse_dt(r["timestamp"]), reverse=True)
        return {
            "alerts": rows[:limit],
            "total": len(rows),
            "alerting_configured": alerts_cfg.enabled,
        }

    return router
