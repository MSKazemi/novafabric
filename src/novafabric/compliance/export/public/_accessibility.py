"""Declared accessibility-conformance claim for public disclosures (ADR-0169 D5 / NF-380).

A pure exporter that assembles an accessibility-conformance object over a public disclosure:

* ``declared_standard`` — ``wcag_2_2_aa`` / ``en_301_549_v4_1_1``,
* ``audit_digest`` — a reference to a **declared** accessibility audit (record-only, a digest/ref
  — never the audit contents),
* ``export_format_check`` — whether the exporter emitted an accessible artifact shape (e.g. a tagged
  structure); an assertion about the *export format*, not about the content's real accessibility.

It records a **declared** claim: NovaFabric performs **no** accessibility audit itself (I-4).
Evidence *supports* the claim — it is **never** a ``compliance_guaranteed``: there is no
compliance/audit-performed/verdict field. This first slice assembles the object from given inputs;
the collector that reads the declared audit/format signals from the sealed export is a documented
follow-on.
"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel

from ..provenance import EvidenceSource


class AccessibilityStandard(str, Enum):
    wcag_2_2_aa = "wcag_2_2_aa"
    en_301_549_v4_1_1 = "en_301_549_v4_1_1"


class AccessibilityClaim(BaseModel):
    declared_standard: str  # an AccessibilityStandard value — a declared claim, never a guarantee
    audit_digest: str | None = None  # ref to a declared audit (record-only) — never the contents
    export_format_check: bool = False  # exporter emitted an accessible shape (export-format only)
    evidence_source: EvidenceSource = EvidenceSource.operator_asserted  # ADR-0197 (I-1)
    # Intentionally NO compliance_guaranteed / audit_performed / verdict field — declared claim.


def build_accessibility_claim(
    *,
    declared_standard: str,
    audit_digest: str | None = None,
    export_format_check: bool = False,
) -> AccessibilityClaim:
    """Assemble a declared accessibility-conformance claim.

    Requires a valid ``declared_standard`` (one of the two :class:`AccessibilityStandard` values).
    ``audit_digest`` is an optional record-only reference to a *declared* audit;
    ``export_format_check`` asserts the export format (not the content) is accessible-shaped.
    NovaFabric performs no audit and guarantees no conformance — this records a declared claim only.
    """
    if not declared_standard:
        raise ValueError("declared_standard is required")
    try:
        std = AccessibilityStandard(declared_standard)
    except ValueError as exc:
        allowed = ", ".join(s.value for s in AccessibilityStandard)
        raise ValueError(
            f"unknown declared_standard {declared_standard!r}; expected one of: {allowed}"
        ) from exc

    return AccessibilityClaim(
        declared_standard=std.value,
        audit_digest=audit_digest,
        export_format_check=export_format_check,
    )
