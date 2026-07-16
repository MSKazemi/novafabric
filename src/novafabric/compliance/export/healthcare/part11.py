"""21 CFR Part 11 electronic-records/signatures evidence artifact (ADR-0160 D1/D2 / NF-282).

A pure exporter over already-sealed evidence: it renders the Part 11 electronic-records elements a
run recorded — signer identity, the §11.50 signing intent, the DSSE signature binding, record
integrity, the trusted (RFC 3161) timestamp, and the audit trail — each ``complete`` / ``partial``
/ ``missing`` with a source ref or a machine-readable reason. It **renders facts, never a
conformity determination**: NovaFabric does not perform a Part 11 assessment; a qualified human
does. Nothing is fabricated (a missing element carries no ref).

Per ADR-0160 the binding **medical-honesty banner** is carried in every artifact (and must be
printed in every CLI output). This first slice is the pure exporter over supplied element refs; the
collector that reads them from the sealed capsule (the §11.50 `SigningIntent`, the DSSE seal, the
RFC 3161 TSR when present) is a documented follow-on.
"""
from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel

#: Binding compliance & medical-honesty banner (ADR-0160 — "in every artifact and CLI output").
MEDICAL_HONESTY_BANNER = (
    "This artifact SUPPORTS a qualified human's regulated determination; it does not guarantee "
    "safety, compliance, or de-identification sufficiency, does not perform a conformity "
    "assessment or Expert Determination, does not transmit anything to a regulator, and does "
    "not constitute "
    "clinical advice or a medical determination. NovaFabric is not a medical device and is not "
    "clinical decision support software: it records and renders sealed facts; a qualified human "
    "makes every regulated and clinical call."
)

#: Version-pinned standard tag.
PART11_STANDARD = "21 CFR Part 11 (Electronic Records; Electronic Signatures)"

#: The Part 11 evidence elements this artifact renders, in fixed order.
PART11_ELEMENTS: tuple[str, ...] = (
    "signer_identity",
    "signing_intent",
    "signature_binding",
    "record_integrity",
    "trusted_timestamp",
    "audit_trail",
)

_MISSING_REASON = "no {element} evidence found in sealed capsules"


class Part11Field(BaseModel):
    element: str
    status: str  # "complete" | "partial" | "missing"
    source_ref: str | None = None  # capsule-fact ref (absent when missing — never fabricated)
    reason: str | None = None


class Part11Record(BaseModel):
    standard: str
    banner: str  # the binding medical-honesty banner — always present
    capsule_root: str
    fields: list[Part11Field]
    summary: dict[str, int]
    # Intentionally NO conformity/verdict/determination field — renders facts, never a Part 11 call.


def build_part11_record(
    *,
    capsule_root: str,
    elements: Mapping[str, str] = {},
    partial: Mapping[str, str] | None = None,
) -> Part11Record:
    """Render a Part 11 electronic-records evidence artifact from per-element source refs.

    Each element present in ``elements`` is ``complete`` (with its source ref); an element also
    named in ``partial`` (a name → reason map) is ``partial``; an element in neither is ``missing``
    with a reason. Nothing is fabricated and no conformity determination is made.
    """
    partial_map = partial or {}
    fields: list[Part11Field] = []
    for name in PART11_ELEMENTS:
        if name in partial_map:
            fields.append(Part11Field(
                element=name, status="partial",
                source_ref=elements.get(name), reason=partial_map[name],
            ))
        elif name in elements:
            fields.append(Part11Field(element=name, status="complete", source_ref=elements[name]))
        else:
            fields.append(Part11Field(
                element=name, status="missing", reason=_MISSING_REASON.format(element=name),
            ))

    summary = {"complete": 0, "partial": 0, "missing": 0}
    for f in fields:
        summary[f.status] += 1
    return Part11Record(
        standard=PART11_STANDARD,
        banner=MEDICAL_HONESTY_BANNER,
        capsule_root=capsule_root,
        fields=fields,
        summary=summary,
    )
