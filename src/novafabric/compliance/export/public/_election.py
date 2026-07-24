"""Election/democratic-process disclosure (ADR-0169 D5 / NF-379, first slice).

A pure exporter that assembles a content-provenance + agent-evidence record for AI-generated
political/civic content:

* ``content_ref`` — the political/civic content the disclosure is about,
* ``provenance_receipt_ref`` — binds an NF-094 / C2PA / SynthID provenance receipt **by digest**,
* ``disclosure_label`` — ``ai_generated`` / ``ai_assisted`` / ``synthetic_media``,
* ``capsule_refs`` — the sealed runs that produced or handled the content (order preserved).

It records and exports a *disclosure* — and it **adjudicates nothing** (I-4): it **MUST NOT** decide
whether the content is lawful, deceptive, or election-regulated. There is deliberately no
lawful/deceptive/verdict field; the label states *what was recorded* about provenance, never a legal
conclusion. This first slice assembles the record from supplied refs; the collector that reads the
provenance receipt from the sealed content is a documented follow-on.
"""
from __future__ import annotations

from collections.abc import Sequence
from enum import Enum

from pydantic import BaseModel

from ..provenance import EvidenceSource


class DisclosureLabel(str, Enum):
    ai_generated = "ai_generated"
    ai_assisted = "ai_assisted"
    synthetic_media = "synthetic_media"


class ElectionDisclosure(BaseModel):
    content_ref: str
    provenance_receipt_ref: str  # binds an NF-094 / C2PA / SynthID receipt by digest
    disclosure_label: str  # a DisclosureLabel value — provenance, never a lawfulness verdict
    capsule_refs: list[str] = []  # sealed runs that produced/handled the content (order preserved)
    evidence_source: EvidenceSource = EvidenceSource.operator_asserted  # ADR-0197 (I-1)
    # Intentionally NO lawful / deceptive / election_regulated / verdict field — decides nothing.


def build_election_disclosure(
    *,
    content_ref: str,
    provenance_receipt_ref: str,
    disclosure_label: str,
    capsule_refs: Sequence[str] = (),
) -> ElectionDisclosure:
    """Assemble an election/democratic-process disclosure from supplied refs.

    Requires ``content_ref``, ``provenance_receipt_ref``, and a valid ``disclosure_label`` (one of
    the three :class:`DisclosureLabel` values). Makes no lawfulness/deception determination — it
    binds the provenance receipt and records the label, nothing more. ``capsule_refs`` order kept.
    """
    if not content_ref:
        raise ValueError("content_ref is required")
    if not provenance_receipt_ref:
        raise ValueError("provenance_receipt_ref is required")
    try:
        label = DisclosureLabel(disclosure_label)
    except ValueError as exc:
        allowed = ", ".join(d.value for d in DisclosureLabel)
        raise ValueError(
            f"unknown disclosure_label {disclosure_label!r}; expected one of: {allowed}"
        ) from exc

    return ElectionDisclosure(
        content_ref=content_ref,
        provenance_receipt_ref=provenance_receipt_ref,
        disclosure_label=label.value,
        capsule_refs=list(capsule_refs),
    )
