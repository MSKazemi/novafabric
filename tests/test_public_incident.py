"""ADR-0169 D1 / NF-378 — public-interest incident disclosure.

A public-audience incident summary assembled from a sealed NF-269/ADR-0088 ``Incident``:
``incident_ref``, ``public_summary``, ``affected_scope`` (**aggregate, no per-subject data**),
``remediation_ref`` — always a **DRAFT, never transmitted** (I-3: never publishes/notifies). It
adjudicates nothing — there is no ``compliance_guaranteed`` claim.
"""
from __future__ import annotations

import pytest

from novafabric.compliance.export.public._public_incident import (
    PublicIncidentDisclosure,
    build_public_incident_disclosure,
)


def test_valid_disclosure_is_draft():
    rec = build_public_incident_disclosure(
        incident_ref="incident://root#i1",
        public_summary="An automated triage agent misclassified a subset of applications.",
        affected_scope="approximately 12,000 applications in region X",
        remediation_ref="capsule://root/fix#r1",
    )
    assert isinstance(rec, PublicIncidentDisclosure)
    assert rec.draft is True
    assert rec.incident_ref == "incident://root#i1"
    assert rec.affected_scope == "approximately 12,000 applications in region X"


def test_draft_is_always_true_even_if_caller_says_false():
    rec = build_public_incident_disclosure(incident_ref="i", draft=False)
    assert rec.draft is True  # never transmitted — draft cannot be cleared here


@pytest.mark.parametrize(
    "field,bad_value",
    [
        ("affected_scope", "applicant SSN 123-45-6789 affected"),
        ("public_summary", "affected: jane.doe@example.com and others"),
        ("affected_scope", "passport_number AB12345 holders"),
    ],
)
def test_per_subject_identifiers_are_rejected(field, bad_value):
    kwargs = {"incident_ref": "i", field: bad_value}
    with pytest.raises(ValueError) as exc:
        build_public_incident_disclosure(**kwargs)
    assert field in str(exc.value)


def test_aggregate_scope_is_accepted():
    rec = build_public_incident_disclosure(
        incident_ref="i", affected_scope="~5% of users in the EU region"
    )
    assert rec.affected_scope == "~5% of users in the EU region"


def test_missing_incident_ref_raises():
    with pytest.raises(ValueError):
        build_public_incident_disclosure(incident_ref="")


def test_no_compliance_guarantee_or_publish_field():
    for forbidden in (
        "compliance_guaranteed", "verdict", "published", "notified", "transmitted", "compliant",
    ):
        assert forbidden not in PublicIncidentDisclosure.model_fields
