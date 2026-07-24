"""Wired lifecycle event sources — ADR-0137 (experimental).

Companion to :mod:`novafabric.events.sources`, which wires the ``ops.*``
alerting family (ADR-0192). This module wires the *lifecycle* family: facts
about what happened to an asset or a capsule, emitted to whatever sink the
operator configured, and a no-op when none is.

Same discipline as the alerting sources:

- **byte-identical to a no-op unless a sink is configured** — the emitter
  short-circuits (ADR-0137: off by default);
- **fail-safe** — emission never raises into the caller, because a NovaFabric
  eventing problem must not break a promotion or a retention sweep;
- **emitted at the decision, not at the side effect**, so a `--force`
  override still produces the event.

Coverage is deliberately partial and the gap is enforced rather than
implied: ``tests/events/test_lifecycle_sources.py`` fails CI if an
``EventType`` is neither wired here nor listed there as a documented
exclusion with a reason.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _safe_emit(
    event_type: str, payload: dict[str, Any], *, subject_kind: str, subject_ref: str
) -> None:
    """Emit one lifecycle event, swallowing everything.

    Imported locally so nothing on a hot path pays for the eventing module
    unless an event is actually being emitted.
    """
    try:
        from novafabric.events.emitter import emit_lifecycle_event  # noqa: PLC0415

        emit_lifecycle_event(
            event_type=event_type,
            subject_kind=subject_kind,
            subject_ref=subject_ref,
            payload=payload,
        )
    except Exception:  # noqa: BLE001 — eventing must never break the caller
        logger.warning("lifecycle event emission failed (%s)", event_type, exc_info=True)


def emit_policy_failed(
    *, asset_id: str, reason: str, decision_id: str, forced: bool
) -> None:
    """`policy.failed` — a policy gate denied an action (ADR-0008).

    Emitted on the **deny itself**, not on the resulting exception, so a
    `--force` override still produces the event: a gate that was overridden
    is exactly the thing a downstream consumer needs to know about. The
    ``forced`` flag distinguishes the two without needing a second type.
    """
    _safe_emit(
        "policy.failed",
        {
            "asset_id": asset_id,
            "reason": reason,
            "decision_id": decision_id,
            "forced": forced,
        },
        subject_kind="policy",
        subject_ref=asset_id,
    )


def emit_retention_applied(
    *,
    item_id: str,
    item_kind: str,
    action: str,
    binding_id: str | None = None,
    reason: str | None = None,
) -> None:
    """`retention.applied` — a retention decision actually took effect.

    Only for the APPLIED outcome. Skipped/held/dry-run/error sweeps are not
    "applied" and emitting them would make a consumer counting deletions
    over-count — the one number a retention consumer is most likely to trust.
    """
    _safe_emit(
        "retention.applied",
        {
            "item_id": item_id,
            "item_kind": item_kind,
            "action": action,
            "binding_id": binding_id,
            "reason": reason,
        },
        subject_kind="retention",
        subject_ref=item_id,
    )
