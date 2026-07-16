"""ADR-0166 D3 — conformance map → OSCAL conformance-*receipt* (first slice).

Binds goal/strategy nodes to named external clauses ({node_id, standard, clause_id,
claim_digest}) and emits an OSCAL assessment-results-shaped **receipt**: a statement that
*this argument was assembled against these clauses*, never that the clauses are *met*.
"""
from __future__ import annotations

from novafabric.assure.case import AssuranceCase, AssuranceNode
from novafabric.assure.conformance import (
    ConformanceMap,
    ConformanceMapEntry,
    Standard,
    conformance_receipt,
)

DIGEST = "c" * 64


def _case() -> AssuranceCase:
    return AssuranceCase(
        case_id="case-1",
        nodes=[
            AssuranceNode(id="G1", type="goal", statement="acceptably safe", supported_by=["Sn1"]),
            AssuranceNode(id="Sn1", type="solution", statement="evidence"),
        ],
    )


def _map() -> ConformanceMap:
    return ConformanceMap(
        entries=[
            ConformanceMapEntry(
                node_id="G1", standard=Standard.eu_ai_act, clause_id="Art.15", claim_digest=DIGEST
            )
        ]
    )


def test_receipt_is_a_receipt_not_a_conformance_verdict():
    receipt = conformance_receipt(_case(), _map())
    assert receipt["receipt_type"] == "conformance-receipt"
    # It must disclaim being an attestation that the clause is met.
    assert "not" in receipt["disclaimer"].lower()
    assert receipt["standards"] == ["eu_ai_act"]


def test_each_mapped_node_becomes_an_observation():
    receipt = conformance_receipt(_case(), _map())
    obs = receipt["observations"]
    assert len(obs) == 1
    assert obs[0] == {
        "node_id": "G1",
        "standard": "eu_ai_act",
        "clause_id": "Art.15",
        "claim_digest": DIGEST,
    }


def test_mapping_a_node_absent_from_the_case_is_a_gap_not_an_observation():
    cmap = ConformanceMap(
        entries=[
            ConformanceMapEntry(
                node_id="GHOST", standard=Standard.nist_ai_rmf, clause_id="MG-1", claim_digest=DIGEST
            )
        ]
    )
    receipt = conformance_receipt(_case(), cmap)
    assert receipt["observations"] == []
    assert any(g["node_id"] == "GHOST" and g["issue"] == "unknown_node" for g in receipt["gaps"])


def test_unsupported_leaf_under_the_argument_is_reported_as_a_gap():
    # Sn1 is a solution with no resolvable evidence — a real gap in the receipt.
    receipt = conformance_receipt(_case(), _map(), unsupported_leaves=["Sn1"])
    assert any(g["node_id"] == "Sn1" and g["issue"] == "unsupported_leaf" for g in receipt["gaps"])


def test_receipt_is_deterministic():
    a = conformance_receipt(_case(), _map())
    b = conformance_receipt(_case(), _map())
    assert a == b


def test_standards_list_is_sorted_and_deduped():
    cmap = ConformanceMap(
        entries=[
            ConformanceMapEntry(node_id="G1", standard=Standard.nist_ai_rmf, clause_id="x", claim_digest=DIGEST),
            ConformanceMapEntry(node_id="G1", standard=Standard.eu_ai_act, clause_id="y", claim_digest=DIGEST),
            ConformanceMapEntry(node_id="G1", standard=Standard.eu_ai_act, clause_id="z", claim_digest=DIGEST),
        ]
    )
    receipt = conformance_receipt(_case(), cmap)
    assert receipt["standards"] == ["eu_ai_act", "nist_ai_rmf"]
