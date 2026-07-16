"""Conformance map → OSCAL conformance-*receipt* (ADR-0166 D3, first slice).

A ``conformance_map`` binds ``goal``/``strategy`` nodes to named external standard
clauses — ``{node_id, standard, clause_id, claim_digest}`` — recording *which clause a
node argues toward*, **never that the clause is met**. The output is a
**conformance-receipt** shaped like an OSCAL assessment-results document: each mapped
node becomes an ``observation``; anything that can't be observed (a mapping to a node the
case doesn't contain, or an unsupported evidence leaf) becomes a ``gap``.

This is deliberately *not* a certificate or a verdict — it is auditable evidence that an
argument was assembled against a named standard's clauses.
"""
from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel

from novafabric.assure.case import AssuranceCase


class Standard(str, Enum):
    iso_iec_42001 = "iso_iec_42001"
    iso_iec_42005 = "iso_iec_42005"
    ul_4600 = "ul_4600"
    eu_ai_act = "eu_ai_act"
    nist_ai_rmf = "nist_ai_rmf"
    iso_iec_ieee_15026 = "iso_iec_ieee_15026"


class ConformanceMapEntry(BaseModel):
    node_id: str
    standard: Standard
    clause_id: str  # the clause a node argues toward (a reference, never the clause body)
    claim_digest: str  # digest binding the node's claim


class ConformanceMap(BaseModel):
    entries: list[ConformanceMapEntry] = []


_DISCLAIMER = (
    "This is a conformance-receipt: it records that this argument was assembled against the "
    "named standard clauses. It is NOT an attestation that any clause is met, nor a certificate."
)


def conformance_receipt(
    case: AssuranceCase,
    conformance_map: ConformanceMap,
    *,
    unsupported_leaves: list[str] | tuple[str, ...] = (),
) -> dict[str, Any]:
    """Render a deterministic OSCAL-shaped conformance-receipt.

    * Each map entry whose node exists in ``case`` → an ``observation``.
    * An entry whose node is absent from ``case`` → a ``gap`` (``unknown_node``).
    * Each id in ``unsupported_leaves`` → a ``gap`` (``unsupported_leaf``).
    """
    known_ids = {n.id for n in case.nodes}

    observations: list[dict[str, str]] = []
    gaps: list[dict[str, str]] = []
    for entry in conformance_map.entries:
        if entry.node_id in known_ids:
            observations.append(
                {
                    "node_id": entry.node_id,
                    "standard": entry.standard.value,
                    "clause_id": entry.clause_id,
                    "claim_digest": entry.claim_digest,
                }
            )
        else:
            gaps.append({"node_id": entry.node_id, "issue": "unknown_node"})

    for leaf in unsupported_leaves:
        gaps.append({"node_id": leaf, "issue": "unsupported_leaf"})

    standards = sorted({e.standard.value for e in conformance_map.entries})

    return {
        "receipt_type": "conformance-receipt",
        "disclaimer": _DISCLAIMER,
        "case_id": case.case_id,
        "standards": standards,
        "observations": observations,
        "gaps": gaps,
    }
