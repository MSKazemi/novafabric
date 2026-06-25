"""Energy conservation / completeness check (ADR-0093 §A4).

Reconciles the sum of *measured* attributed joules against an independently
measured node total (e.g. Slurm ``sacct`` energy, or an integrated RAPL/IPMI
total). Divergence beyond a threshold is flagged ``diverged``; when no
independent total exists the status is the honest ``unmeasurable`` — never a
silently reconciled number.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from novafabric.energy._receipt import Confidence, EnergyReceipt
from novafabric.evidence.intoto import dsse_sign, make_intoto_statement

CONSERVATION_PREDICATE_TYPE = "https://novafabric.io/energy-conservation/v0"

ConservationStatus = Literal["balanced", "diverged", "unmeasurable"]

#: confidences that contribute a real measured figure to the attributed sum
_MEASURED_CONFIDENCES = {Confidence.MEASURED, Confidence.APPORTIONED}


class EnergyConservationAssertion(BaseModel):
    """Signed statement that attributed energy reconciles with the node total."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = "0.1.0"
    run_id: str
    generated_at: str
    node_measured_joules: float | None
    attributed_joules_sum: float
    idle_baseline_j: float
    unattributed_joules: float | None
    divergence_pct: float | None
    threshold_pct: float
    status: ConservationStatus
    receipts_total: int
    receipts_measured: int
    receipts_declared: int
    receipts_unavailable: int


def compute_conservation(
    receipts: Iterable[EnergyReceipt],
    *,
    run_id: str,
    node_measured_joules: float | None = None,
    idle_baseline_j: float = 0.0,
    threshold_pct: float = 15.0,
    generated_at: str | None = None,
) -> EnergyConservationAssertion:
    receipts = list(receipts)
    attributed = sum(
        r.measured_joules or 0.0
        for r in receipts
        if r.confidence in _MEASURED_CONFIDENCES and r.measured_joules is not None
    )
    n_measured = sum(1 for r in receipts if r.confidence in _MEASURED_CONFIDENCES)
    n_declared = sum(1 for r in receipts if r.confidence is Confidence.DECLARED)
    n_unavailable = sum(1 for r in receipts if r.confidence is Confidence.UNKNOWN)

    if node_measured_joules is None:
        status: ConservationStatus = "unmeasurable"
        divergence: float | None = None
        unattributed: float | None = None
    else:
        unattributed = node_measured_joules - (attributed + idle_baseline_j)
        denom = node_measured_joules if node_measured_joules else 1.0
        divergence = abs(unattributed) / denom * 100.0
        status = "diverged" if divergence > threshold_pct else "balanced"

    return EnergyConservationAssertion(
        run_id=run_id,
        generated_at=generated_at or datetime.now(timezone.utc).isoformat(),
        node_measured_joules=node_measured_joules,
        attributed_joules_sum=attributed,
        idle_baseline_j=idle_baseline_j,
        unattributed_joules=unattributed,
        divergence_pct=divergence,
        threshold_pct=threshold_pct,
        status=status,
        receipts_total=len(receipts),
        receipts_measured=n_measured,
        receipts_declared=n_declared,
        receipts_unavailable=n_unavailable,
    )


def attest_conservation(
    assertion: EnergyConservationAssertion, signer: Any
) -> dict[str, Any]:
    """DSSE-sign a conservation assertion (in-toto, subject = run id digest)."""
    import hashlib

    digest = hashlib.sha256(assertion.run_id.encode()).hexdigest()
    statement = make_intoto_statement(
        predicate_type=CONSERVATION_PREDICATE_TYPE,
        subject_name=f"run-capsule/{assertion.run_id}",
        subject_sha256=digest,
        predicate=assertion.model_dump(),
    )
    envelope: dict[str, Any] = dsse_sign(statement, signer)
    return envelope


__all__ = [
    "CONSERVATION_PREDICATE_TYPE",
    "ConservationStatus",
    "EnergyConservationAssertion",
    "attest_conservation",
    "compute_conservation",
]
