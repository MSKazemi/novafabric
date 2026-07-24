"""ADR-0170 D5 / NF-387 — premium-relevant governance-control attestation export.

Maps a declared control catalog to the **shipped** NovaFabric governance evidence present for a
capsule (sealing, HITL/oversight, eval-gated promotion, redaction proofs) — each control marked
``evidenced`` / ``not_evidenced`` / ``declared``. It **presents** governance evidence for an insurer
to reason over; it does **not** certify that a control is adequate.
"""
from __future__ import annotations

from novafabric.compliance.export.control_attestation import (
    GOVERNANCE_EVIDENCE_KINDS,
    ControlAttestationPack,
    ControlStatus,
    build_control_attestation,
)
from novafabric.compliance.export.provenance import EvidenceSource


def test_evidence_source_marks_every_control():
    # ADR-0197 phase 2, I-1: this exporter is a pure projection over supplied
    # governance-evidence refs — it re-performs no binding — so an evidenced
    # control is operator_asserted, not capsule_verified (never overclaim).
    pack = build_control_attestation(
        capsule_root="r" * 64, catalog=_CATALOG, present_evidence=_PRESENT, declared=["GOV-4"]
    )
    by_id = {e.control_id: e for e in pack.entries}
    assert by_id["GOV-1"].evidence_source is EvidenceSource.operator_asserted  # evidenced
    assert by_id["GOV-4"].evidence_source is EvidenceSource.operator_asserted  # declared
    # not_evidenced = a checked gap (evidence expected, absent) → unverifiable (I-2)
    assert by_id["GOV-3"].evidence_source is EvidenceSource.unverifiable
    for e in pack.entries:
        assert e.evidence_source is not None

_CATALOG = [
    {"control_id": "GOV-1", "evidence_kind": "sealing"},
    {"control_id": "GOV-2", "evidence_kind": "eval_gated_promotion"},
    {"control_id": "GOV-3", "evidence_kind": "redaction"},  # absent below → not_evidenced
    {"control_id": "GOV-4"},  # operator-declared (no NovaFabric evidence kind)
]
_PRESENT = {
    "sealing": "capsule://root/seal#d1",
    "eval_gated_promotion": "capsule://root/promote#d2",
}


def test_evidenced_controls_carry_the_present_ref():
    pack = build_control_attestation(
        capsule_root="r" * 64, catalog=_CATALOG, present_evidence=_PRESENT, declared=["GOV-4"]
    )
    assert isinstance(pack, ControlAttestationPack)
    e = next(x for x in pack.entries if x.control_id == "GOV-1")
    assert e.status == ControlStatus.evidenced
    assert e.evidence_ref == "capsule://root/seal#d1"  # a ref, never a value


def test_absent_evidence_is_not_evidenced():
    pack = build_control_attestation(
        capsule_root="r" * 64, catalog=_CATALOG, present_evidence=_PRESENT, declared=["GOV-4"]
    )
    e = next(x for x in pack.entries if x.control_id == "GOV-3")
    assert e.status == ControlStatus.not_evidenced
    assert e.evidence_ref is None


def test_operator_declared_control_is_declared():
    pack = build_control_attestation(
        capsule_root="r" * 64, catalog=_CATALOG, present_evidence=_PRESENT, declared=["GOV-4"]
    )
    e = next(x for x in pack.entries if x.control_id == "GOV-4")
    assert e.status == ControlStatus.declared
    assert e.evidence_ref is None


def test_declared_set_overrides_absent_evidence():
    # An operator can declare a control even if NovaFabric has no evidence for it.
    pack = build_control_attestation(
        capsule_root="r" * 64, catalog=_CATALOG, present_evidence=_PRESENT, declared=["GOV-3"]
    )
    e = next(x for x in pack.entries if x.control_id == "GOV-3")
    assert e.status == ControlStatus.declared


def test_summary_counts():
    pack = build_control_attestation(
        capsule_root="r" * 64, catalog=_CATALOG, present_evidence=_PRESENT, declared=["GOV-4"]
    )
    assert pack.summary["evidenced"] == 2
    assert pack.summary["not_evidenced"] == 1
    assert pack.summary["declared"] == 1


def test_governance_evidence_kinds_are_the_shipped_surfaces():
    assert {"sealing", "oversight", "eval_gated_promotion", "redaction"} <= set(
        GOVERNANCE_EVIDENCE_KINDS
    )


def test_presents_not_certifies_no_verdict_field():
    for forbidden in ("certified", "adequate", "passed", "verdict", "compliant", "approved"):
        assert forbidden not in ControlAttestationPack.model_fields


def test_empty_catalog():
    pack = build_control_attestation(capsule_root="r" * 64, catalog=[])
    assert pack.entries == []
    assert pack.summary == {"evidenced": 0, "not_evidenced": 0, "declared": 0}
