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
import re
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, Response

from novafabric._oneline import PATH_LIMIT, one_line
from novafabric.serve.http_cache import conditional_json

logger = logging.getLogger(__name__)

# Audit `details["outcome"]` (slice-1 writes "delivered"/"error") → the public
# outcome enum. Anything already public passes straight through.
_OUTCOME_MAP = {"error": "failed"}

_EPOCH = datetime.min.replace(tzinfo=timezone.utc)

#: Same bound `nova verify` applies before it records a run id (issue #276): no
#: ``/`` and never only dots, so the id cannot address another API path.
_RUN_ID_RE = re.compile(r"^(?!\.+\Z)[A-Za-z0-9._:@-]{1,200}\Z")

_EMPTY_LINK: dict[str, Any] = {
    "claimed_run_id": None,
    "capsule_dir": None,
    "first_divergence": None,
}


def alert_run_link(source: Any) -> dict[str, Any]:
    """Triage refs from an alert's details/payload (issue #276), all nullable.

    Additive row fields for the dashboard's triage panel; today only
    ``ops.seal.verify_failed`` records them, and an absent key is ``None`` —
    nothing is derived from the subject.

    - ``capsule_dir``: the directory that failed verification (what to re-verify).
    - ``claimed_run_id``: the run id the failed capsule's own ``capsule.yaml``
      names. **Unverified** — the seal that failed no longer vouches for that
      manifest, so a tampered capsule can name any run.
    - ``first_divergence``: file-granularity refs only (path, reason, stream,
      divergent_files).
    """
    if not isinstance(source, dict):
        return dict(_EMPTY_LINK)
    raw_run = source.get("claimed_run_id")
    claimed = raw_run if isinstance(raw_run, str) and _RUN_ID_RE.match(raw_run) else None
    raw_dir = source.get("capsule_dir")
    capsule_dir = one_line(raw_dir, PATH_LIMIT) if isinstance(raw_dir, str) and raw_dir else None
    raw_fd = source.get("first_divergence")
    first_divergence: dict[str, Any] | None = None
    if isinstance(raw_fd, dict) and isinstance(raw_fd.get("path"), str):
        count = raw_fd.get("divergent_files")
        stream = raw_fd.get("stream")
        first_divergence = {
            "path": one_line(raw_fd["path"], PATH_LIMIT),
            "reason": one_line(raw_fd.get("reason") or "", 32),
            "stream": one_line(stream, 64) if isinstance(stream, str) else None,
            "divergent_files": (
                count if isinstance(count, int) and not isinstance(count, bool) else None
            ),
        }
    return {
        "claimed_run_id": claimed,
        "capsule_dir": capsule_dir,
        "first_divergence": first_divergence,
    }


def parse_alert_ts(value: str) -> datetime:
    """Best-effort ISO8601 → aware datetime for sorting; epoch on failure."""
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return _EPOCH


def delivery_rows(
    audit_path: Path, limit: int
) -> tuple[list[dict[str, Any]], set[str]]:
    """Up to *limit* newest `alert.delivery` audit rows + covered event ids.

    Bounded tail read (ADR-0199 shared helper): scans backwards from EOF in
    64 KiB blocks and stops once *limit* delivery rows are collected, so IO
    stays O(limit-ish blocks) instead of re-reading the whole hash-chained
    log on every request.
    """
    from pydantic import ValidationError  # noqa: PLC0415

    from novafabric.audit import AuditEntry, AuditEventType  # noqa: PLC0415
    from novafabric.serve.audit import tail_lines  # noqa: PLC0415

    rows: list[dict[str, Any]] = []
    covered: set[str] = set()
    for _off, raw in tail_lines(audit_path):
        try:
            entry = AuditEntry.model_validate_json(raw)
        except ValidationError:
            continue
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
            **alert_run_link(details),
        })
        if len(rows) >= limit:
            break
    return rows, covered


def emitted_rows(
    events_path: Path, covered: set[str], limit: int
) -> list[dict[str, Any]]:
    """Up to *limit* newest `emitted` rows for `ops.*` events lacking delivery.

    Same bounded tail read as :func:`delivery_rows` — newest-first from EOF,
    stopping at *limit* rows rather than scanning the whole events log.
    """
    from novafabric.serve.audit import tail_lines  # noqa: PLC0415

    rows: list[dict[str, Any]] = []
    for _off, raw in tail_lines(events_path):
        try:
            rec = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(rec, dict):
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
            **alert_run_link(rec.get("payload")),
        })
        if len(rows) >= limit:
            break
    return rows


def build_alerts_router(
    verify_token: Callable[..., Any],
    *,
    db_path: Path | None,
) -> APIRouter:
    router = APIRouter(dependencies=[Depends(verify_token)], tags=["alerts"])

    @router.get("/api/alerts/recent")
    async def recent_alerts(
        request: Request,
        limit: int = Query(default=50, ge=1, le=200),
    ) -> Response:
        from novafabric.audit import resolve_audit_log_path  # noqa: PLC0415
        from novafabric.events.alerts import load_alerts_config_from_env  # noqa: PLC0415
        from novafabric.events.emitter import load_config_from_env  # noqa: PLC0415

        alerts_cfg = load_alerts_config_from_env()
        events_cfg = load_config_from_env()
        audit_path = alerts_cfg.audit_log_path or resolve_audit_log_path()

        # Scan cap per source: 2×limit keeps IO bounded while leaving headroom
        # so `total` stays exact for any log within the tail window (and the
        # merged sort still has more than `limit` candidates to pick from).
        scan_cap = 2 * limit
        rows: list[dict[str, Any]] = []
        covered: set[str] = set()
        try:
            rows, covered = delivery_rows(audit_path, scan_cap)
        except Exception as exc:  # noqa: BLE001 — a read must never 500
            logger.warning("reading alert audit log failed: %s", exc)
        if events_cfg.log_path is not None:
            try:
                rows.extend(emitted_rows(events_cfg.log_path, covered, scan_cap))
            except Exception as exc:  # noqa: BLE001 — a read must never 500
                logger.warning("reading events log failed: %s", exc)

        rows.sort(key=lambda r: parse_alert_ts(r["timestamp"]), reverse=True)
        payload = {
            "alerts": rows[:limit],
            "total": len(rows),
            "alerting_configured": alerts_cfg.enabled,
        }
        return conditional_json(request, payload, max_age=15)

    return router
