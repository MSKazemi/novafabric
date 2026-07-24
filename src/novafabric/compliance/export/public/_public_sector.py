"""Public-sector agentic-AI disclosure record (ADR-0169 D1 / NF-373, first slice).

A pure **DRAFT** public-audience disclosure *document* assembled from references to sealed evidence.
Unlike the internal RAI system card (the E7 track), this is a public-sector disclosure a public body
submits, and NovaFabric **references, never re-authors or asserts**:

* ``authority_ref`` is a *declared* reference to the declaring public body — never an assertion by
  NovaFabric that the body approved anything,
* ``system_card_ref`` binds an E7 system card **by digest**; its body is never re-authored here,
* ``capsule_refs`` are digests of the sealed runs the disclosure summarizes.

Each required field backed by nothing is listed in ``manual_completion_required`` and **never
fabricated**; the output ``status`` is always ``DRAFT`` (NovaFabric never registers/publishes/
transmits — the external-action approval gate lives outside this exporter). This first slice is the
pure assembler over supplied references; the collector that gathers the run digests from the sealed
capsule set is a documented follow-on.
"""
from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel

from ..provenance import EvidenceSource

#: Required fields for a public-sector disclosure record (per NF-373).
PUBLIC_SECTOR_REQUIRED: tuple[str, ...] = (
    "authority_ref",
    "agent_ref",
    "decision_scope",
    "human_oversight_ref",
    "capsule_refs",
)


class PublicSectorDisclosure(BaseModel):
    status: str = "DRAFT"  # NovaFabric never registers/publishes/transmits
    authority_ref: str | None = None  # declared reference to the public body — never an assertion
    agent_ref: str | None = None
    decision_scope: str | None = None  # a declared scope string — never findings/PII
    human_oversight_ref: str | None = None
    capsule_refs: list[str] = []  # digests of the sealed runs summarized
    system_card_ref: str | None = None  # references an E7 system card by digest — never re-authored
    manual_completion_required: list[str] = []  # required fields with no value (never invented)
    # ADR-0197: pure crosswalk, re-performs no binding → document is operator_asserted (I-1);
    # every manual_completion_required entry is a checked gap → unverifiable (I-2).
    evidence_source: EvidenceSource = EvidenceSource.operator_asserted
    manual_evidence_source: EvidenceSource = EvidenceSource.unverifiable


def build_public_sector_disclosure(
    *,
    authority_ref: str | None = None,
    agent_ref: str | None = None,
    decision_scope: str | None = None,
    human_oversight_ref: str | None = None,
    capsule_refs: Sequence[str] = (),
    system_card_ref: str | None = None,
) -> PublicSectorDisclosure:
    """Assemble a DRAFT public-sector disclosure record from supplied references.

    Any of the five required fields (``authority_ref``, ``agent_ref``, ``decision_scope``,
    ``human_oversight_ref``, ``capsule_refs``) left empty is listed in
    ``manual_completion_required`` and never fabricated. ``system_card_ref`` is optional and, when
    present, binds the E7 card by digest — its body is never re-authored here.
    """
    refs = list(capsule_refs)
    values: dict[str, object] = {
        "authority_ref": authority_ref,
        "agent_ref": agent_ref,
        "decision_scope": decision_scope,
        "human_oversight_ref": human_oversight_ref,
        "capsule_refs": refs,
    }
    manual = [name for name in PUBLIC_SECTOR_REQUIRED if not values[name]]
    return PublicSectorDisclosure(
        authority_ref=authority_ref,
        agent_ref=agent_ref,
        decision_scope=decision_scope,
        human_oversight_ref=human_oversight_ref,
        capsule_refs=refs,
        system_card_ref=system_card_ref,
        manual_completion_required=manual,
    )
