"""Wired `ops.*` alert sources — ADR-0192 (experimental).

One emitter per `ops.*` event type, so the payload shape for a given alert
is defined once rather than copied into each call site. Every emitter is:

- **byte-identical to a no-op unless alerting is configured** — the router
  short-circuits when no ``NOVA_ALERTS_*`` endpoint is set (ADR-0192: off by
  default);
- **fail-safe** — ``emit_ops_alert`` never raises, and call sites wrap it
  anyway, because a NovaFabric alerting problem must never break the user's
  workload (a standing anti-pattern in this codebase);
- **backgrounded on request-serving paths** so endpoint latency never lands
  on a request the operator is waiting for.

Severity assignment (ADR-0192 D1) is deliberately conservative and stated
here rather than at each call site:

- ``critical`` — an evidence or recoverability guarantee is already broken:
  a failed seal verification, a failed backup. These mean "you may not be
  able to prove or restore something", which is this product's whole point.
- ``warning`` — a guardrail fired as designed: sustained rate limiting, a
  denied policy gate, detected drift. The system behaved correctly; a human
  should look, but nothing is broken.

Quota breach keeps its ``critical`` severity and lives at its call site in
``server/quotas.py`` (the first wired source, ADR-0192 slice 1).
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _safe_emit(
    *,
    event_type: str,
    severity: str,
    subject_ref: str,
    payload: dict[str, Any],
    source: str,
    background: bool = False,
) -> None:
    """Emit one ops alert, swallowing everything.

    The import is local so that nothing on a hot path pays for the alerting
    module unless an alert is actually being emitted.
    """
    try:
        from novafabric.events.alerts import emit_ops_alert  # noqa: PLC0415

        emit_ops_alert(
            event_type=event_type,
            severity=severity,
            subject_ref=subject_ref,
            payload=payload,
            source=source,
            background=background,
        )
    except Exception:  # noqa: BLE001 — alerting must never break the caller
        logger.warning("ops alert emission failed (%s)", event_type, exc_info=True)


def emit_rate_limit_sustained_alert(payload: dict[str, Any]) -> None:
    """`ops.rate_limit.sustained` — a client stayed over its limit (ADR-0179).

    A guardrail working as designed, so ``warning``: it usually means a
    misconfigured client or a load change, not a broken system. Backgrounded
    — this rides the request-rejection path.
    """
    _safe_emit(
        event_type="ops.rate_limit.sustained",
        severity="warning",
        subject_ref=f"rate_limit:{payload.get('limit_class', 'unknown')}",
        payload={
            "limit_class": payload.get("limit_class"),
            "rejected_count": payload.get("rejected_count"),
            "window_start": payload.get("window_start"),
            # The hashed key only — never the raw API key or client id.
            "key_hash": payload.get("key_hash"),
        },
        source="nova server",
        background=True,
    )


def emit_policy_violation_alert(
    *, asset_id: str, decision_reason: str, stage: str, source: str
) -> None:
    """`ops.policy.violation` — a policy gate denied an action (ADR-0008).

    ``warning``, not ``critical``: a deny is the gate doing its job. The
    alert exists so a human learns that someone is repeatedly hitting a
    gate, not because the system is unhealthy.
    """
    _safe_emit(
        event_type="ops.policy.violation",
        severity="warning",
        subject_ref=f"asset:{asset_id}",
        payload={"asset_id": asset_id, "reason": decision_reason, "stage": stage},
        source=source,
    )


def emit_drift_detected_alert(
    *, kind: str, label: str, value: float, threshold: float
) -> None:
    """`ops.drift.detected` — a drift statistic crossed its threshold (ADR-0147)."""
    _safe_emit(
        event_type="ops.drift.detected",
        severity="warning",
        subject_ref=f"drift:{kind}:{label}",
        payload={
            "kind": kind,
            "label": label,
            "value": value,
            "threshold": threshold,
        },
        source="nova drift",
    )


def emit_seal_verify_failed_alert(
    *, capsule_id: str, errors: list[str], signature_ok: bool | None = None
) -> None:
    """`ops.seal.verify_failed` — a capsule's seal did not verify.

    ``critical``: this is the evidence guarantee itself failing. Either the
    capsule was altered or the trust chain is broken; both mean the run can
    no longer be proven. Errors are truncated — an alert is a notification,
    not a report.
    """
    _safe_emit(
        event_type="ops.seal.verify_failed",
        severity="critical",
        subject_ref=f"capsule:{capsule_id or 'unknown'}",
        payload={
            "capsule_id": capsule_id,
            "signature_ok": signature_ok,
            "errors": errors[:5],
            "error_count": len(errors),
        },
        source="nova verify",
    )


def emit_backup_failed_alert(
    *, operation: str, target: str, reason: str
) -> None:
    """`ops.backup.failed` — a backup create or verify failed (ADR-0181).

    ``critical``: a backup you cannot take or cannot verify is a
    recoverability guarantee that is already gone, and it is usually
    discovered at the worst possible moment. This is exactly the class of
    silent failure operational alerting exists for.
    """
    _safe_emit(
        event_type="ops.backup.failed",
        severity="critical",
        subject_ref=f"backup:{target}",
        payload={"operation": operation, "target": target, "reason": reason},
        source="nova backup",
    )
