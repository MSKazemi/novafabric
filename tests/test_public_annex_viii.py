"""ADR-0169 D1 (first slice) — EU AI Act Annex VIII public-DB entry exporter (NF-371).

A **DRAFT** crosswalk over sealed evidence: each Annex VIII required field is marked
``capsule_evidence`` (backed by a sealed capsule fact — carried as a digest/ref, never the raw
bytes) or ``operator_declared`` (the operator's public declaration); a required field backed by
neither is **listed as unmapped, never fabricated**. NovaFabric never registers/publishes — the
status is always ``DRAFT``.
"""
from __future__ import annotations

from novafabric.compliance.export.public.annex_viii import (
    ANNEX_VIII_REQUIRED,
    ANNEX_VIII_STANDARD,
    AnnexVIIIEntry,
    FieldSource,
    build_annex_viii_entry,
)


def test_status_is_always_draft_and_standard_is_version_pinned():
    entry = build_annex_viii_entry(capsule_root="r" * 64)
    assert isinstance(entry, AnnexVIIIEntry)
    assert entry.status == "DRAFT"
    assert entry.standard == ANNEX_VIII_STANDARD
    assert "2024/1689" in entry.standard  # version-pinned regulation


def test_capsule_evidence_field_carries_a_ref_not_a_value():
    entry = build_annex_viii_entry(
        capsule_root="r" * 64,
        capsule_evidence={"provider_name": "capsule://root/provider#digest"},
    )
    f = next(x for x in entry.fields if x.name == "provider_name")
    assert f.source is FieldSource.capsule_evidence
    assert f.evidence_ref == "capsule://root/provider#digest"
    assert f.value is None  # never the raw bytes


def test_operator_declared_field_carries_the_public_value():
    entry = build_annex_viii_entry(
        capsule_root="r" * 64,
        operator_declared={"system_trade_name": "Acme Underwriter"},
    )
    f = next(x for x in entry.fields if x.name == "system_trade_name")
    assert f.source is FieldSource.operator_declared
    assert f.value == "Acme Underwriter"
    assert f.evidence_ref is None


def test_capsule_evidence_takes_precedence_over_declaration():
    entry = build_annex_viii_entry(
        capsule_root="r" * 64,
        operator_declared={"provider_name": "declared"},
        capsule_evidence={"provider_name": "capsule://root/provider"},
    )
    f = next(x for x in entry.fields if x.name == "provider_name")
    assert f.source is FieldSource.capsule_evidence  # a sealed fact beats a declaration


def test_unmapped_required_fields_are_listed_never_fabricated():
    entry = build_annex_viii_entry(capsule_root="r" * 64)  # nothing supplied
    assert set(entry.unmapped_required) == set(ANNEX_VIII_REQUIRED)
    assert entry.fields == []  # nothing invented


def test_fields_follow_the_fixed_required_order():
    entry = build_annex_viii_entry(
        capsule_root="r" * 64,
        operator_declared={k: "x" for k in ANNEX_VIII_REQUIRED},
    )
    assert [f.name for f in entry.fields] == list(ANNEX_VIII_REQUIRED)


def test_capsule_root_is_carried():
    assert build_annex_viii_entry(capsule_root="abc").capsule_root == "abc"
