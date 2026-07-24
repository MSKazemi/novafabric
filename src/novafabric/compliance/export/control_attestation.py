"""Premium-relevant governance-control attestation export (ADR-0170 D5 / NF-387).

A pure exporter that renders premium-relevant governance controls as an attestation pack: it maps a
**declared control catalog** to the **shipped** NovaFabric governance evidence present for a capsule
— sealing, HITL/oversight facets, eval-gated promotion, redaction proofs — marking each control:

* ``evidenced`` — the control's mapped governance evidence is present (carried as a ref),
* ``not_evidenced`` — the control's evidence is absent (an honest gap, never fabricated),
* ``declared`` — the operator asserts the control without NovaFabric evidence backing it.

Insurers grant affirmative cover on documented governance evidence; this exporter **presents** that
evidence for an insurer to reason over — it does **not** certify a control is adequate. There is
deliberately no certified/adequate/pass/verdict field. It carries references and digests only, never
claimant PII or financial records.

This maps only **already-shipped** governance surfaces; the ``facets.risk_transfer`` capture facet
(ADR-0170 D1–D4) is a separate, not-yet-captured concern and is not read here.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import Enum

from pydantic import BaseModel

from .provenance import EvidenceSource

#: The shipped NovaFabric governance-evidence surfaces a control can map to.
GOVERNANCE_EVIDENCE_KINDS: tuple[str, ...] = (
    "sealing",
    "oversight",
    "eval_gated_promotion",
    "redaction",
)


class ControlStatus(str, Enum):
    evidenced = "evidenced"
    not_evidenced = "not_evidenced"
    declared = "declared"


#: ADR-0197 provenance for each control status. This exporter is a pure projection
#: over supplied governance-evidence refs — it re-performs no binding — so an
#: ``evidenced`` control is ``operator_asserted`` (its ref is carried, not
#: re-performed), never ``capsule_verified``. A ``not_evidenced`` control is a
#: checked gap (evidence expected for its mapped kind, absent) → ``unverifiable``.
_STATUS_PROVENANCE: dict[ControlStatus, EvidenceSource] = {
    ControlStatus.evidenced: EvidenceSource.operator_asserted,
    ControlStatus.declared: EvidenceSource.operator_asserted,
    ControlStatus.not_evidenced: EvidenceSource.unverifiable,
}


class ControlAttestationEntry(BaseModel):
    control_id: str
    evidence_kind: str | None = None  # the governance surface this control maps to, if any
    status: ControlStatus
    evidence_ref: str | None = None  # ref to the present evidence (evidenced only) — never a value
    evidence_source: EvidenceSource | None = None  # ADR-0197 provenance marker (I-1)


class ControlAttestationPack(BaseModel):
    capsule_root: str
    catalog_ref: str | None = None  # reference to the declared control catalog, if named
    entries: list[ControlAttestationEntry]
    summary: dict[str, int]
    # Intentionally NO certified/adequate/pass/verdict field — presents evidence, never certifies.


def build_control_attestation(
    *,
    capsule_root: str,
    catalog: Sequence[Mapping[str, str]],
    present_evidence: Mapping[str, str] = {},
    declared: Sequence[str] = (),
    catalog_ref: str | None = None,
) -> ControlAttestationPack:
    """Map a declared control catalog to the governance evidence present for a capsule.

    Each catalog control is ``{control_id, evidence_kind?}``. A control the operator ``declared`` is
    ``declared``; otherwise a control whose ``evidence_kind`` is in ``present_evidence`` is
    ``evidenced`` (carrying that ref); anything else is ``not_evidenced``. Presents evidence — never
    certifies a control is adequate.
    """
    declared_set = set(declared)
    entries: list[ControlAttestationEntry] = []
    summary = {"evidenced": 0, "not_evidenced": 0, "declared": 0}
    for control in catalog:
        cid = control["control_id"]
        ekind = control.get("evidence_kind")
        ref: str | None = None
        if cid in declared_set:
            status = ControlStatus.declared
        elif ekind and ekind in present_evidence:
            status = ControlStatus.evidenced
            ref = present_evidence[ekind]
        else:
            status = ControlStatus.not_evidenced
        entries.append(ControlAttestationEntry(
            control_id=cid, evidence_kind=ekind, status=status, evidence_ref=ref,
            evidence_source=_STATUS_PROVENANCE[status],
        ))
        summary[status.value] += 1

    return ControlAttestationPack(
        capsule_root=capsule_root,
        catalog_ref=catalog_ref,
        entries=entries,
        summary=summary,
    )
