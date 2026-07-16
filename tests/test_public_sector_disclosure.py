"""ADR-0169 D1 / NF-373 — public-sector agentic-AI disclosure record.

A DRAFT public-audience disclosure *document* that assembles references to sealed runs — it
**references, never re-authors** (a system card is bound by digest, an ``authority_ref`` is a
*declared* reference to the public body, never a NovaFabric assertion). Required fields backed by
nothing are listed for manual completion — never fabricated. Output is always a DRAFT.
"""
from __future__ import annotations

from novafabric.compliance.export.public._public_sector import (
    PUBLIC_SECTOR_REQUIRED,
    PublicSectorDisclosure,
    build_public_sector_disclosure,
)


def test_complete_record_has_no_manual_gaps():
    rec = build_public_sector_disclosure(
        authority_ref="gov://body/dwp",
        agent_ref="capsule://root/agent#a1",
        decision_scope="benefit-eligibility triage",
        human_oversight_ref="capsule://root/hitl#h1",
        capsule_refs=["capsule://root/run1#d1", "capsule://root/run2#d2"],
        system_card_ref="card://e7#digest",
    )
    assert isinstance(rec, PublicSectorDisclosure)
    assert rec.status == "DRAFT"
    assert rec.authority_ref == "gov://body/dwp"
    assert rec.capsule_refs == ["capsule://root/run1#d1", "capsule://root/run2#d2"]
    assert rec.system_card_ref == "card://e7#digest"
    assert rec.manual_completion_required == []


def test_missing_required_fields_are_listed_never_fabricated():
    rec = build_public_sector_disclosure(
        agent_ref="capsule://root/agent#a1",
        decision_scope="triage",
    )
    # authority_ref, human_oversight_ref, capsule_refs absent → listed, and the fields stay empty.
    assert rec.authority_ref is None
    assert rec.human_oversight_ref is None
    assert rec.capsule_refs == []
    for f in ("authority_ref", "human_oversight_ref", "capsule_refs"):
        assert f in rec.manual_completion_required
    assert "agent_ref" not in rec.manual_completion_required


def test_empty_capsule_refs_counts_as_missing():
    rec = build_public_sector_disclosure(
        authority_ref="gov://x",
        agent_ref="a",
        decision_scope="s",
        human_oversight_ref="h",
        capsule_refs=[],
    )
    assert "capsule_refs" in rec.manual_completion_required


def test_system_card_ref_is_optional_and_ref_only():
    rec = build_public_sector_disclosure(
        authority_ref="gov://x",
        agent_ref="a",
        decision_scope="s",
        human_oversight_ref="h",
        capsule_refs=["c1"],
    )
    assert rec.system_card_ref is None
    assert "system_card_ref" not in rec.manual_completion_required  # optional, not required


def test_required_set_is_the_five_spec_fields():
    assert set(PUBLIC_SECTOR_REQUIRED) == {
        "authority_ref", "agent_ref", "decision_scope", "human_oversight_ref", "capsule_refs",
    }


def test_no_reauthor_or_assertion_or_verdict_field():
    # NovaFabric references, never re-authors or adjudicates.
    for forbidden in (
        "system_card_body", "authority_assertion", "verdict", "lawful", "compliant", "grade",
    ):
        assert forbidden not in PublicSectorDisclosure.model_fields
