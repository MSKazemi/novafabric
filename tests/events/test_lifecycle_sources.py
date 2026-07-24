"""ADR-0137 lifecycle event sources, and a coverage guard over ALL EventTypes.

The emitter and sinks shipped complete, but almost nothing emitted: only
`capsule.created` and `capsule.validated` had call sites, so the feature was
largely inert. This wires `policy.failed` and `retention.applied`, and adds a
guard that forces every future `EventType` to be either wired or explicitly
declared unwired with a reason — so the gap can never quietly grow again.
"""

from __future__ import annotations

from typing import Any

import pytest

from novafabric.events import lifecycle_sources
from novafabric.events.model import EventType


@pytest.fixture()
def emitted(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def _fake(**kwargs: Any) -> None:
        calls.append(kwargs)

    monkeypatch.setattr("novafabric.events.emitter.emit_lifecycle_event", _fake)
    return calls


# ---------------------------------------------------------------------------
# Emitter shapes
# ---------------------------------------------------------------------------


def test_policy_failed_shape(emitted: list[dict[str, Any]]) -> None:
    lifecycle_sources.emit_policy_failed(
        asset_id="model-a@1.0.0",
        reason="eval score below threshold",
        decision_id="dec-1",
        forced=False,
    )
    (call,) = emitted
    assert call["event_type"] == "policy.failed"
    assert call["subject_kind"] == "policy"
    assert call["subject_ref"] == "model-a@1.0.0"
    assert call["payload"]["decision_id"] == "dec-1"
    assert call["payload"]["forced"] is False


def test_policy_failed_records_a_forced_override(emitted: list[dict[str, Any]]) -> None:
    """A gate that was overridden is exactly what a consumer needs to see."""
    lifecycle_sources.emit_policy_failed(
        asset_id="a@1", reason="r", decision_id="d", forced=True
    )
    assert emitted[0]["payload"]["forced"] is True


def test_retention_applied_shape(emitted: list[dict[str, Any]]) -> None:
    lifecycle_sources.emit_retention_applied(
        item_id="run-1", item_kind="capsule", action="delete", binding_id="b1"
    )
    (call,) = emitted
    assert call["event_type"] == "retention.applied"
    assert call["subject_kind"] == "retention"
    assert call["payload"]["action"] == "delete"


def test_emission_failure_never_reaches_the_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An eventing fault must not break a promotion or a retention sweep."""

    def _explode(**_kwargs: Any) -> None:
        raise RuntimeError("sink is down")

    monkeypatch.setattr("novafabric.events.emitter.emit_lifecycle_event", _explode)
    lifecycle_sources.emit_policy_failed(
        asset_id="a@1", reason="r", decision_id="d", forced=False
    )
    lifecycle_sources.emit_retention_applied(
        item_id="i", item_kind="capsule", action="delete"
    )


# ---------------------------------------------------------------------------
# Coverage guard over every EventType
# ---------------------------------------------------------------------------

#: Types wired to a real call site, and where. Keep in step with the code.
WIRED: dict[str, str] = {
    "capsule.created": "capture/orchestrator.py",
    "capsule.validated": "cli/validate.py",
    "policy.failed": "registry/service.py (promote policy gate)",
    "retention.applied": "retention/actions.py (_record, APPLIED only)",
    # ops.* family — wired via events/sources.py (ADR-0192), covered by
    # tests/events/test_ops_alert_sources.py.
    "ops.quota.breached": "server/quotas.py",
    "ops.rate_limit.sustained": "server/rate_limit.py",
    "ops.policy.violation": "registry/service.py",
    "ops.drift.detected": "cli/drift.py",
    "ops.seal.verify_failed": "cli/verify.py",
    "ops.backup.failed": "cli/backup.py",
}

#: Declared but deliberately NOT wired yet, each with the reason. Adding a
#: type here is a decision, not a shrug — it should name what is missing.
UNWIRED_WITH_REASON: dict[str, str] = {
    "promotion.proposed": (
        "maker-checker propose path; the semantic emit point is inside the "
        "approval workflow, not the registry write. Needs the workflow read "
        "properly rather than an emission guessed at a plausible line."
    ),
    "promotion.approved": (
        "same workflow as promotion.proposed — wire the pair together so they "
        "cannot disagree about what 'approved' means."
    ),
    "promotion.rejected": (
        "the maker-checker rejection path; must be wired together with "
        "proposed/approved so the three cannot disagree about the transition."
    ),
    "promotion.bypass.created": (
        "bypass lifecycle lives in promote/bundle_store.py, which is a storage "
        "layer; emitting there would report a write, not a decision."
    ),
    "promotion.bypass.approved": (
        "same storage-layer objection as promotion.bypass.created — the "
        "approval decision happens above bundle_store, and that is where it "
        "should be observed from."
    ),
    "promotion.bypass.expired": (
        "expiry is time-derived, not an action — needs a decision on whether "
        "it is emitted lazily on read or by a sweep."
    ),
    "webhook.ping": (
        "synthetic per-subscription test event, emitted only by the server "
        "webhook dispatcher's targeted ping path (ADR-0205, "
        "server/webhook_dispatch.py) — deliberately not a lifecycle_sources "
        "emission: it must bypass event-type filters and reach exactly one "
        "hook."
    ),
}


def test_every_event_type_is_wired_or_explicitly_deferred() -> None:
    """A new EventType must not sit silently unemitted.

    ADR-0137 shipped a complete emitter with almost no call sites, which made
    the feature look implemented while emitting nothing. This guard makes that
    state impossible to reach by accident.
    """
    declared = {e.value for e in EventType}
    accounted = set(WIRED) | set(UNWIRED_WITH_REASON)

    unaccounted = sorted(declared - accounted)
    assert not unaccounted, (
        f"EventType(s) neither wired nor explicitly deferred: {unaccounted}. "
        "Wire them in events/lifecycle_sources.py (or events/sources.py for "
        "ops.*), or add them to UNWIRED_WITH_REASON here with a real reason."
    )

    stale = sorted(accounted - declared)
    assert not stale, f"accounted-for types that no longer exist: {stale}"


def test_deferred_entries_carry_a_real_reason() -> None:
    """Guard the guard: an empty reason would defeat the point."""
    for event_type, reason in UNWIRED_WITH_REASON.items():
        assert len(reason) > 40, f"{event_type} needs a substantive reason, got {reason!r}"
