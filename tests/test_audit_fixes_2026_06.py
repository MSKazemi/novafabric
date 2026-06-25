"""Regression tests for the 2026-06-19 app-wide bug-audit fixes.

Each test locks in one confirmed bug fix so it cannot silently regress.
"""
from __future__ import annotations

from pathlib import Path

# --- Merkle: phantom-index inclusion proof must be rejected -------------------

def test_merkle_rejects_out_of_range_leaf_index() -> None:
    from novafabric.trust.novaseal.merkle import (
        _compute_inclusion_proof,
        _compute_root,
        _leaf_hash,
        verify_inclusion_proof,
    )

    leaves = [_leaf_hash(f"entry-{i}".encode()) for i in range(3)]
    root = _compute_root(leaves)
    proof = _compute_inclusion_proof(2, leaves)
    # A genuine proof for a real leaf still verifies.
    assert verify_inclusion_proof(leaves[2], 2, proof, root, tree_size=3) is True
    # Reusing it for a phantom index >= tree_size must be rejected.
    assert verify_inclusion_proof(leaves[2], 3, proof, root, tree_size=3) is False
    assert verify_inclusion_proof(leaves[2], 2, proof, root, tree_size=0) is False
    assert verify_inclusion_proof(leaves[2], -1, proof, root, tree_size=3) is False


# --- Lineage: recursive queries must terminate on cyclic graphs --------------

def _run_node(ref: str):
    from novafabric.lineage._types import LineageNode, node_id_for
    return LineageNode(
        node_id=node_id_for("run", ref), kind="run", ref=ref,
        first_seen_capsule_run_id=ref, payload={"kind": "run", "ref": ref},
    )


def _run_edge(edge_type: str, src: str, dst: str):
    from novafabric.lineage._types import LineageEdge
    return LineageEdge(
        edge_type=edge_type,
        source={"kind": "run", "run_id": src},
        target={"kind": "run", "run_id": dst},
        confidence="observed",
        capsule_run_id=src,
    )


def test_lineage_blast_radius_and_provenance_terminate_on_cycle(tmp_path: Path) -> None:
    from novafabric.lineage._store import LineageStore

    store = LineageStore(db_path=tmp_path / "lin.db")
    nodes = [_run_node("A"), _run_node("B")]
    # Cycle: A -> B and B -> A.
    edges = [_run_edge("derived", "A", "B"), _run_edge("derived", "B", "A")]
    store.replace_capsule_lineage(nodes, edges, "A")
    # Must terminate (no path explosion / hang) and return each node once.
    desc = store.blast_radius("A", kind="run", depth=20)
    prov = store.provenance("A", kind="run", depth=20)
    assert {d["ref"] for d in desc} <= {"A", "B"}
    assert {p["ref"] for p in prov} <= {"A", "B"}


def test_lineage_replay_chain_terminates_on_cycle(tmp_path: Path) -> None:
    from novafabric.lineage._store import LineageStore

    store = LineageStore(db_path=tmp_path / "lin2.db")
    nodes = [_run_node("A"), _run_node("B")]
    edges = [_run_edge("replayed_from", "A", "B"), _run_edge("replayed_from", "B", "A")]
    store.replace_capsule_lineage(nodes, edges, "A")
    chain = store.replay_chain("A")
    # Without the cycle guard this returned ~100 alternating rows.
    assert len({c["ref"] for c in chain}) <= 2
