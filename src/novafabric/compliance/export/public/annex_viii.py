"""EU AI Act Annex VIII public-DB entry exporter (ADR-0169 D1 / NF-371, first slice).

A pure **DRAFT** crosswalk over already-sealed evidence: it assembles the EU AI Act Annex VIII /
Art. 71 public-database entry for a high-risk system, marking each required field as
``capsule_evidence`` (backed by a sealed capsule fact — carried as a digest/ref, **never the raw
bytes**) or ``operator_declared`` (the operator's public declaration). A required field backed by
neither is **listed as unmapped, never fabricated**.

Per ADR-0169 D1 the output is a DRAFT the operator submits — NovaFabric never registers, publishes,
or transmits (the external-action approval gate lives outside this exporter), so ``status`` is
always ``DRAFT``. This first slice is the pure crosswalk over supplied field maps; the collector
that reads the field values from the sealed capsule is a documented follow-on.
"""
from __future__ import annotations

from collections.abc import Mapping
from enum import Enum

from pydantic import BaseModel

from ..provenance import EvidenceSource

#: Version-pinned standard tag.
ANNEX_VIII_STANDARD = "EU AI Act Annex VIII / Art. 71 (Regulation (EU) 2024/1689)"

#: Annex VIII Section A required fields for a high-risk-system public-DB entry (fixed order).
ANNEX_VIII_REQUIRED: tuple[str, ...] = (
    "provider_name",
    "provider_contact",
    "system_trade_name",
    "system_intended_purpose",
    "system_status",
    "member_states_available",
    "high_risk_category",
    "conformity_assessment_ref",
)


class FieldSource(str, Enum):
    operator_declared = "operator_declared"
    capsule_evidence = "capsule_evidence"


class AnnexVIIIField(BaseModel):
    name: str
    source: FieldSource
    value: str | None = None  # operator-declared public value (never set for capsule_evidence)
    evidence_ref: str | None = None  # digest/ref into the sealed capsule (capsule_evidence only)
    # ADR-0197: this crosswalk re-performs no binding, so BOTH the operator_declared
    # and the capsule_evidence branch are operator_asserted — a supplied capsule ref
    # is not a re-performed verification. (Distinct from the ``source`` enum above.)
    evidence_source: EvidenceSource = EvidenceSource.operator_asserted


class AnnexVIIIEntry(BaseModel):
    status: str = "DRAFT"  # NovaFabric never registers/publishes/transmits
    standard: str
    capsule_root: str
    fields: list[AnnexVIIIField]
    unmapped_required: list[str]  # required fields with no evidence/declaration (never invented)
    #: ADR-0197 provenance of every entry in ``unmapped_required``: a required field
    #: with neither evidence nor declaration is a checked gap → ``unverifiable`` (I-2).
    unmapped_evidence_source: EvidenceSource = EvidenceSource.unverifiable


def build_annex_viii_entry(
    *,
    capsule_root: str,
    operator_declared: Mapping[str, str] = {},
    capsule_evidence: Mapping[str, str] = {},
) -> AnnexVIIIEntry:
    """Assemble a DRAFT Annex VIII public-DB entry from per-field sources.

    For each required field, a sealed ``capsule_evidence`` ref takes precedence over an
    ``operator_declared`` value; a field backed by neither is listed in ``unmapped_required`` and
    never fabricated. Capsule-evidence fields carry only the ref — never the raw value.
    """
    fields: list[AnnexVIIIField] = []
    unmapped: list[str] = []
    for name in ANNEX_VIII_REQUIRED:
        if name in capsule_evidence:
            fields.append(AnnexVIIIField(
                name=name, source=FieldSource.capsule_evidence,
                evidence_ref=capsule_evidence[name],
            ))
        elif name in operator_declared:
            fields.append(AnnexVIIIField(
                name=name, source=FieldSource.operator_declared,
                value=operator_declared[name],
            ))
        else:
            unmapped.append(name)
    return AnnexVIIIEntry(
        standard=ANNEX_VIII_STANDARD,
        capsule_root=capsule_root,
        fields=fields,
        unmapped_required=unmapped,
    )
