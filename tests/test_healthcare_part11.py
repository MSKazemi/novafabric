"""ADR-0160 D1/D2 (first slice) — 21 CFR Part 11 electronic-records evidence artifact (NF-282).

A pure exporter over sealed evidence: it renders the Part 11 electronic-records/signatures elements
a run recorded (signer identity, §11.50 signing intent, DSSE signature binding, record integrity,
trusted timestamp, audit trail), each ``complete`` / ``partial`` / ``missing``. It **renders facts,
never a conformity determination** — a qualified human makes the Part 11 call — and every artifact
carries the binding medical-honesty banner (ADR-0160, "in every artifact and CLI output").
"""
from __future__ import annotations

from novafabric.compliance.export.healthcare.part11 import (
    MEDICAL_HONESTY_BANNER,
    PART11_ELEMENTS,
    PART11_STANDARD,
    Part11Record,
    build_part11_record,
)


def test_banner_is_always_present_and_binding():
    rec = build_part11_record(capsule_root="r" * 64)
    assert isinstance(rec, Part11Record)
    assert rec.banner == MEDICAL_HONESTY_BANNER
    assert "not a medical device" in rec.banner.lower()


def test_standard_is_version_pinned():
    rec = build_part11_record(capsule_root="r" * 64)
    assert rec.standard == PART11_STANDARD
    assert "Part 11" in rec.standard


def test_present_element_is_complete_with_ref():
    rec = build_part11_record(
        capsule_root="r" * 64,
        elements={"trusted_timestamp": "capsule://root/tsr#digest"},
    )
    ts = next(f for f in rec.fields if f.element == "trusted_timestamp")
    assert ts.status == "complete"
    assert ts.source_ref == "capsule://root/tsr#digest"


def test_missing_element_is_missing_with_reason_and_no_ref():
    rec = build_part11_record(capsule_root="r" * 64)
    ts = next(f for f in rec.fields if f.element == "trusted_timestamp")
    assert ts.status == "missing"
    assert ts.source_ref is None
    assert ts.reason


def test_no_conformity_or_verdict_field():
    for forbidden in ("conformity", "verdict", "compliant", "pass", "determination"):
        assert forbidden not in Part11Record.model_fields


def test_elements_follow_the_fixed_order():
    rec = build_part11_record(
        capsule_root="r" * 64, elements={k: "x" for k in PART11_ELEMENTS}
    )
    assert [f.element for f in rec.fields] == list(PART11_ELEMENTS)


def test_summary_counts():
    rec = build_part11_record(
        capsule_root="r" * 64,
        elements={"signer_identity": "a", "signing_intent": "b"},
    )
    assert rec.summary["complete"] == 2
    assert rec.summary["missing"] == len(PART11_ELEMENTS) - 2
