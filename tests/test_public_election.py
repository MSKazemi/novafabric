"""ADR-0169 D5 / NF-379 — election/democratic-process disclosure.

A content-provenance + agent-evidence record for AI-generated political/civic content: ``content_ref``,
``provenance_receipt_ref`` (binds an NF-094 / C2PA / SynthID receipt **by digest**), ``disclosure_label``
(a three-value enum), and ``capsule_refs``. It records a *disclosure*; it **MUST NOT** determine whether
content is lawful, deceptive, or election-regulated (I-4) — there is no lawfulness verdict.
"""
from __future__ import annotations

import pytest

from novafabric.compliance.export.public._election import (
    DisclosureLabel,
    ElectionDisclosure,
    build_election_disclosure,
)


def test_valid_disclosure_binds_receipt():
    rec = build_election_disclosure(
        content_ref="content://ad#c1",
        provenance_receipt_ref="receipt://nf094#digest",
        disclosure_label="ai_generated",
        capsule_refs=["capsule://root/run1#d1"],
    )
    assert isinstance(rec, ElectionDisclosure)
    assert rec.content_ref == "content://ad#c1"
    assert rec.provenance_receipt_ref == "receipt://nf094#digest"
    assert rec.disclosure_label == "ai_generated"
    assert rec.capsule_refs == ["capsule://root/run1#d1"]


@pytest.mark.parametrize("label", ["ai_generated", "ai_assisted", "synthetic_media"])
def test_all_three_labels_accepted(label):
    rec = build_election_disclosure(
        content_ref="c", provenance_receipt_ref="r", disclosure_label=label
    )
    assert rec.disclosure_label == label


def test_invalid_label_rejected():
    with pytest.raises(ValueError):
        build_election_disclosure(
            content_ref="c", provenance_receipt_ref="r", disclosure_label="deepfake"
        )


def test_capsule_refs_order_preserved():
    order = ["z#1", "a#2", "m#3"]
    rec = build_election_disclosure(
        content_ref="c", provenance_receipt_ref="r",
        disclosure_label="ai_assisted", capsule_refs=order,
    )
    assert rec.capsule_refs == order


def test_missing_required_raise():
    with pytest.raises(ValueError):
        build_election_disclosure(
            content_ref="", provenance_receipt_ref="r", disclosure_label="ai_generated"
        )
    with pytest.raises(ValueError):
        build_election_disclosure(
            content_ref="c", provenance_receipt_ref="", disclosure_label="ai_generated"
        )


def test_no_lawfulness_or_verdict_field():
    for forbidden in ("lawful", "deceptive", "election_regulated", "verdict", "compliant", "legal"):
        assert forbidden not in ElectionDisclosure.model_fields


def test_disclosure_label_enum_values():
    assert {m.value for m in DisclosureLabel} == {"ai_generated", "ai_assisted", "synthetic_media"}
