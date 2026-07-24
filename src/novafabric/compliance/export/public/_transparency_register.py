"""Algorithmic-transparency-register crosswalk (ADR-0169 D1 / NF-372, first slice).

A pure **DRAFT** crosswalk over already-sealed evidence into an algorithm-register record shape,
selected by ``standard`` and **standard-version-pinned**: the UK **ATRS** (Algorithmic Transparency
Recording Standard) record, and the **Amsterdam** and **Helsinki** algorithm-register records.
Each required field is marked ``capsule_evidence`` (backed by a sealed capsule fact — carried as a
digest/ref, **never the raw bytes**) or ``operator_declared`` (the operator's public declaration);
a required field backed by neither is listed in ``manual_completion_required``, never fabricated.

Per ADR-0169 D1 the output is a DRAFT the operator submits to GOV.UK ATRS or a city register —
NovaFabric never registers, publishes, or transmits (the external-action approval gate lives outside
this exporter), so ``status`` is always ``DRAFT``. This first slice is the pure crosswalk over
supplied field maps; the collector that reads the field values from the sealed capsule is a
documented follow-on. The field sets are register-shaped starting points, not an official schema —
anything unmapped is surfaced for manual completion rather than invented.
"""
from __future__ import annotations

from collections.abc import Mapping
from enum import Enum

from pydantic import BaseModel

from ..provenance import EvidenceSource


class RegisterStandard(str, Enum):
    atrs = "atrs"  # UK Algorithmic Transparency Recording Standard
    amsterdam = "amsterdam"  # Amsterdam algoritmeregister
    helsinki = "helsinki"  # Helsinki AI register


#: Version-pinned standard tag per register (recorded as the version this crosswalk emitted).
REGISTER_STANDARDS: dict[RegisterStandard, str] = {
    RegisterStandard.atrs: "UK ATRS — Algorithmic Transparency Recording Standard v2.0",
    RegisterStandard.amsterdam: "Amsterdam Algoritmeregister — algorithm-register record (2024)",
    RegisterStandard.helsinki: "Helsinki AI Register — service record (2024)",
}

#: Required fields per register, in fixed order (a register-shaped starting point, never official).
REGISTER_REQUIRED: dict[RegisterStandard, tuple[str, ...]] = {
    RegisterStandard.atrs: (
        "tool_name",
        "tool_description",
        "responsible_organisation",
        "tool_contact",
        "how_the_tool_works",
        "decision_or_support",
        "data_sources",
        "human_oversight",
        "risks_and_mitigations",
    ),
    RegisterStandard.amsterdam: (
        "algorithm_name",
        "algorithm_description",
        "responsible_department",
        "contact",
        "datasets_used",
        "non_discrimination_measures",
        "human_involvement",
        "legal_basis",
    ),
    RegisterStandard.helsinki: (
        "service_name",
        "purpose",
        "datasets_used",
        "automated_decision_making",
        "human_oversight",
        "responsible_person",
    ),
}


class FieldSource(str, Enum):
    operator_declared = "operator_declared"
    capsule_evidence = "capsule_evidence"


class TransparencyRegisterField(BaseModel):
    name: str
    source: FieldSource
    value: str | None = None  # operator-declared public value (never set for capsule_evidence)
    evidence_ref: str | None = None  # digest/ref into the sealed capsule (capsule_evidence only)
    # ADR-0197: pure crosswalk, re-performs no binding → both branches operator_asserted
    # (a supplied capsule ref is not a re-performed verification). Distinct from ``source``.
    evidence_source: EvidenceSource = EvidenceSource.operator_asserted


class TransparencyRegisterRecord(BaseModel):
    status: str = "DRAFT"  # NovaFabric never registers/publishes/transmits
    standard: str  # "atrs" | "amsterdam" | "helsinki"
    standard_version: str  # version-pinned tag of the register this crosswalk emitted
    capsule_root: str
    fields: list[TransparencyRegisterField]
    manual_completion_required: list[str]  # required fields with no evidence/declaration
    #: ADR-0197 provenance of every entry in ``manual_completion_required``: a required
    #: field with neither evidence nor declaration is a checked gap → ``unverifiable`` (I-2).
    manual_evidence_source: EvidenceSource = EvidenceSource.unverifiable


def build_transparency_register(
    *,
    standard: str,
    capsule_root: str,
    operator_declared: Mapping[str, str] = {},
    capsule_evidence: Mapping[str, str] = {},
) -> TransparencyRegisterRecord:
    """Assemble a DRAFT algorithm-register record for ``standard`` from per-field sources.

    ``standard`` selects the register shape (``atrs`` / ``amsterdam`` / ``helsinki``); an unknown
    value raises ``ValueError``. For each required field a sealed ``capsule_evidence`` ref takes
    precedence over an ``operator_declared`` value; a field backed by neither is listed in
    ``manual_completion_required`` and never fabricated. Capsule-evidence fields carry only the ref.
    """
    try:
        std = RegisterStandard(standard)
    except ValueError as exc:
        allowed = ", ".join(s.value for s in RegisterStandard)
        raise ValueError(
            f"unknown transparency register {standard!r}; expected one of: {allowed}"
        ) from exc

    fields: list[TransparencyRegisterField] = []
    manual: list[str] = []
    for name in REGISTER_REQUIRED[std]:
        if name in capsule_evidence:
            fields.append(TransparencyRegisterField(
                name=name, source=FieldSource.capsule_evidence,
                evidence_ref=capsule_evidence[name],
            ))
        elif name in operator_declared:
            fields.append(TransparencyRegisterField(
                name=name, source=FieldSource.operator_declared,
                value=operator_declared[name],
            ))
        else:
            manual.append(name)
    return TransparencyRegisterRecord(
        standard=std.value,
        standard_version=REGISTER_STANDARDS[std],
        capsule_root=capsule_root,
        fields=fields,
        manual_completion_required=manual,
    )
