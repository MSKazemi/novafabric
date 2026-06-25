"""Slice A4 — energy conservation / completeness check (ADR-0093).

The "thermodynamic accountability completeness" claim made mechanically
checkable: attributed joules must reconcile with an independently measured node
total, within a threshold. Divergence is a first-class signed status, never a
silent reconciliation.
"""

from __future__ import annotations

from novafabric.capsule.ulid_util import new_ulid
from novafabric.energy import (
    ActionRef,
    AttributionMethod,
    CarbonAccountingMode,
    Confidence,
    CounterName,
    EnergyReceipt,
    Hardware,
    MeasurementScope,
    MeasurementSource,
    declared_receipt,
    unavailable_receipt,
)
from novafabric.energy._conservation import compute_conservation


def _measured_receipt(joules: float) -> EnergyReceipt:
    return EnergyReceipt(
        action_ref=ActionRef(kind="job_step", id=new_ulid()),
        run_id="run",
        capsule_id="sha256:" + "a" * 64,
        measured_joules=joules,
        measurement_source=MeasurementSource.RAPL_PKG,
        measurement_scope=MeasurementScope.PER_PACKAGE,
        confidence=Confidence.APPORTIONED,
        attribution_method=AttributionMethod.TIME_SHARE,
        carbon_g_co2e=None,
        grid_intensity_g_per_kwh=None,
        carbon_accounting_mode=CarbonAccountingMode.UNKNOWN,
        hardware=Hardware(node_id="n1", counters_available=[CounterName.RAPL_PKG]),
        generated_at="2026-06-19T00:00:00+00:00",
    ).with_payload_hash()


def test_unmeasurable_when_no_independent_node_total():
    receipts = [_measured_receipt(10.0)]
    a = compute_conservation(receipts, run_id="run", node_measured_joules=None)
    assert a.status == "unmeasurable"
    assert a.divergence_pct is None


def test_balanced_within_threshold():
    receipts = [_measured_receipt(40.0), _measured_receipt(55.0)]
    a = compute_conservation(
        receipts, run_id="run", node_measured_joules=100.0, idle_baseline_j=5.0
    )
    # attributed 95 + idle 5 == 100 → 0% divergence
    assert a.status == "balanced"
    assert a.divergence_pct == 0.0
    assert a.attributed_joules_sum == 95.0


def test_diverged_beyond_threshold():
    receipts = [_measured_receipt(10.0)]
    a = compute_conservation(
        receipts, run_id="run", node_measured_joules=100.0, threshold_pct=15.0
    )
    # attributed 10 vs node 100 → 90% divergence
    assert a.status == "diverged"
    assert a.divergence_pct is not None and a.divergence_pct > 15.0


def test_attest_conservation_is_signed_and_verifiable(tmp_path):
    from novafabric.energy._conservation import (
        CONSERVATION_PREDICATE_TYPE,
        attest_conservation,
    )
    from novafabric.evidence.intoto import dsse_verify
    from novafabric.evidence.signing import (
        LocalSigner,
        generate_keypair,
        verify_with_pem,
    )

    priv, _ = generate_keypair(tmp_path)
    signer = LocalSigner(priv)
    assertion = compute_conservation(
        [_measured_receipt(10.0)], run_id="run", node_measured_joules=10.0
    )
    envelope = attest_conservation(assertion, signer)
    statement = dsse_verify(
        envelope, lambda pae, sig: verify_with_pem(signer.public_pem, pae, sig)
    )
    assert statement["predicateType"] == CONSERVATION_PREDICATE_TYPE
    assert statement["predicate"]["status"] == "balanced"


def test_declared_and_unavailable_do_not_count_as_attributed():
    receipts = [
        declared_receipt(
            action_ref=ActionRef(kind="model_call", id=new_ulid()),
            run_id="run",
            capsule_id="sha256:" + "b" * 64,
            node_id="laptop",
            declared_joules=999.0,
        ),
        unavailable_receipt(
            action_ref=ActionRef(kind="model_call", id=new_ulid()),
            run_id="run",
            capsule_id="sha256:" + "c" * 64,
            node_id="n1",
        ),
    ]
    a = compute_conservation(receipts, run_id="run", node_measured_joules=None)
    assert a.attributed_joules_sum == 0.0
    assert a.receipts_declared == 1
    assert a.receipts_unavailable == 1
    assert a.receipts_measured == 0
