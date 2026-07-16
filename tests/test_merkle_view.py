"""ADR-0172 (data slice) — Evidence Provenance Merkle proof-tree projection.

Pure, read-only projection of a sealed capsule's Merkle tree into a node model:
``leaf → intermediate → seal-root → tsr``. Node labels for leaves are the **field path only**
(ADR-0009 — never the value); nodes carry a short hash prefix, node type, and a verify state.

The tree is derived from :func:`merkle_layers`, so the projected structure matches the sealed
root byte-for-byte; this projection never reimplements the hashing.
"""
from __future__ import annotations

from novafabric.trust.merkle_view import (
    NodeType,
    ProofNode,
    VerifyState,
    build_proof_tree,
)
from novafabric.trust.novaseal.merkle import _compute_root, _leaf_hash


def _leaves(n: int) -> list[str]:
    return [_leaf_hash(f"entry-{i}".encode()) for i in range(n)]


def test_node_typing_leaf_intermediate_seal_root():
    tree = build_proof_tree(_leaves(4))
    by_type = {}
    for node in tree.nodes:
        by_type.setdefault(node.node_type, []).append(node)
    assert len(by_type[NodeType.leaf]) == 4
    assert len(by_type[NodeType.intermediate]) == 2
    assert len(by_type[NodeType.seal_root]) == 1


def test_computed_root_matches_novaseal():
    leaves = _leaves(5)
    tree = build_proof_tree(leaves)
    assert tree.computed_root == _compute_root(leaves)


def test_matching_sealed_root_verifies_seal_root():
    leaves = _leaves(4)
    tree = build_proof_tree(leaves, sealed_root=_compute_root(leaves))
    assert tree.sealed is True
    root = next(n for n in tree.nodes if n.node_type is NodeType.seal_root)
    assert root.verify_state is VerifyState.verified


def test_mismatching_sealed_root_flags_mismatch():
    leaves = _leaves(4)
    tree = build_proof_tree(leaves, sealed_root="00" * 32)
    root = next(n for n in tree.nodes if n.node_type is NodeType.seal_root)
    assert root.verify_state is VerifyState.mismatch


def test_unsealed_when_no_sealed_root():
    tree = build_proof_tree(_leaves(2))
    assert tree.sealed is False
    assert all(n.verify_state is VerifyState.unverified for n in tree.nodes)


def test_leaf_labels_are_field_paths():
    leaves = _leaves(2)
    tree = build_proof_tree(leaves, leaf_labels=["capsule.output[0].tool_call", "env.MODEL"])
    leaf_nodes = [n for n in tree.nodes if n.node_type is NodeType.leaf]
    assert leaf_nodes[0].label == "capsule.output[0].tool_call"
    assert leaf_nodes[1].label == "env.MODEL"


def test_tsr_node_added_at_apex():
    leaves = _leaves(2)
    tree = build_proof_tree(leaves, sealed_root=_compute_root(leaves), tsr_hash="ab" * 32)
    tsr = [n for n in tree.nodes if n.node_type is NodeType.tsr]
    assert len(tsr) == 1
    assert tsr[0].label == "RFC 3161 timestamp"


def test_hash_prefix_is_truncated():
    leaves = _leaves(2)
    node = build_proof_tree(leaves).nodes[0]
    full = leaves[0]
    assert node.hash_prefix == f"{full[:6]}..{full[-6:]}"


def test_empty_tree_has_no_nodes():
    tree = build_proof_tree([])
    assert tree.nodes == []
    assert tree.computed_root is None


def test_proofnode_never_carries_a_value():
    # ADR-0009 invariant: nodes carry path/label + hash prefix, never a captured value
    assert "value" not in ProofNode.model_fields
