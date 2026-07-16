"""ADR-0169 D5 / NF-380 — declared accessibility-conformance claim for public disclosures.

An accessibility-conformance object over a public disclosure: a ``declared_standard`` (WCAG 2.2 AA /
EN 301 549), an ``audit_digest`` (reference to a *declared* audit, record-only), and an
``export_format_check`` (did the exporter emit an accessible artifact shape). It records a **declared**
claim — NovaFabric performs **no** accessibility audit itself (I-4). Evidence *supports* the claim;
it is **never** a ``compliance_guaranteed``.
"""
from __future__ import annotations

import pytest

from novafabric.compliance.export.public._accessibility import (
    AccessibilityClaim,
    AccessibilityStandard,
    build_accessibility_claim,
)


def test_valid_wcag_claim():
    rec = build_accessibility_claim(
        declared_standard="wcag_2_2_aa",
        audit_digest="audit://declared#d1",
        export_format_check=True,
    )
    assert isinstance(rec, AccessibilityClaim)
    assert rec.declared_standard == "wcag_2_2_aa"
    assert rec.audit_digest == "audit://declared#d1"
    assert rec.export_format_check is True


@pytest.mark.parametrize("std", ["wcag_2_2_aa", "en_301_549_v4_1_1"])
def test_both_standards_accepted(std):
    rec = build_accessibility_claim(declared_standard=std)
    assert rec.declared_standard == std


def test_invalid_standard_rejected():
    with pytest.raises(ValueError):
        build_accessibility_claim(declared_standard="section_508")


def test_audit_digest_optional_and_format_check_defaults_false():
    rec = build_accessibility_claim(declared_standard="en_301_549_v4_1_1")
    assert rec.audit_digest is None
    assert rec.export_format_check is False  # conservative default — not asserted unless supplied


def test_missing_standard_raises():
    with pytest.raises(ValueError):
        build_accessibility_claim(declared_standard="")


def test_no_compliance_guarantee_or_audit_performed_field():
    # Records a DECLARED claim, never a guarantee, and NovaFabric performs no audit itself.
    for forbidden in (
        "compliance_guaranteed", "audit_performed", "verdict", "compliant", "conformant",
    ):
        assert forbidden not in AccessibilityClaim.model_fields


def test_standard_enum_values():
    assert {m.value for m in AccessibilityStandard} == {"wcag_2_2_aa", "en_301_549_v4_1_1"}
