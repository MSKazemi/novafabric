"""Public-interest incident disclosure (ADR-0169 D1 / NF-378, first slice).

A pure exporter that assembles a **public-audience** incident summary from a sealed NF-269 /
ADR-0088 ``Incident``:

* ``incident_ref`` — the sealed incident the summary is about,
* ``public_summary`` — a plain-language summary for a citizen/press audience,
* ``affected_scope`` — the **aggregate** scope of impact (no per-subject data),
* ``remediation_ref`` — a reference to the recorded remediation.

Two invariants from the ADR are enforced. (1) It is always a **DRAFT, never transmitted** (I-3):
``draft`` is always ``True`` and NovaFabric never publishes or notifies — the external-action gate
lives elsewhere. (2) ``affected_scope`` and ``public_summary`` describe **aggregate** impact, so a
validator rejects any per-subject raw identifier (an SSN, an email, …) that would turn a public
summary into a per-subject disclosure. It **adjudicates nothing** — it records that evidence
supports a summary, and carries no ``compliance_guaranteed`` claim (no such field exists).

This is the public-facing companion to E7's OECD-aligned voluntary incident disclosure (NF-269): E7
targets the incident-reporting framework, this targets the citizen/press audience.
"""
from __future__ import annotations

from pydantic import BaseModel

from ..provenance import EvidenceSource

#: Compound tokens marking per-subject raw identifiers (lowercased match); aggregate text never has
#: these. Curated to avoid false positives on plain-language aggregate summaries.
_PER_SUBJECT_IDENTIFIER_PATTERNS: tuple[str, ...] = (
    "ssn",
    "social_security",
    "social security",
    "passport_number",
    "passport number",
    "credit_card",
    "credit card",
    "national_id",
    "@",  # an email address in a public summary is a per-subject leak
)


class PublicIncidentDisclosure(BaseModel):
    incident_ref: str
    public_summary: str | None = None
    affected_scope: str | None = None  # aggregate impact — never per-subject data
    remediation_ref: str | None = None
    draft: bool = True  # always DRAFT — NovaFabric never publishes/notifies (I-3)
    evidence_source: EvidenceSource = EvidenceSource.operator_asserted  # ADR-0197 (I-1)
    # Intentionally NO compliance_guaranteed / verdict / published field — adjudicates nothing.


def _per_subject_identifiers(text: str) -> list[str]:
    lowered = text.lower()
    return [pat for pat in _PER_SUBJECT_IDENTIFIER_PATTERNS if pat in lowered]


def build_public_incident_disclosure(
    *,
    incident_ref: str,
    public_summary: str | None = None,
    affected_scope: str | None = None,
    remediation_ref: str | None = None,
    draft: bool = True,  # accepted for symmetry but always forced True below
) -> PublicIncidentDisclosure:
    """Assemble a DRAFT public-interest incident disclosure from a sealed incident.

    Requires ``incident_ref``. Rejects (``ValueError``) any per-subject raw identifier in
    ``public_summary`` or ``affected_scope`` — a public summary is aggregate, never per-subject.
    ``draft`` is always forced ``True``: this export is never transmitted.
    """
    if not incident_ref:
        raise ValueError("incident_ref is required")
    for name, value in (("public_summary", public_summary), ("affected_scope", affected_scope)):
        if value and _per_subject_identifiers(value):
            raise ValueError(
                f"{name} must be aggregate: per-subject identifier(s) present "
                f"({', '.join(_per_subject_identifiers(value))}) — never disclose per-subject data"
            )
    return PublicIncidentDisclosure(
        incident_ref=incident_ref,
        public_summary=public_summary,
        affected_scope=affected_scope,
        remediation_ref=remediation_ref,
        draft=True,  # never transmitted — forced regardless of the argument
    )
