"""ADR-0172 (seal-module helper) — `merkle_layers` enumerates the tree layers.

The Merkle proof-tree visualization needs every layer of the tree (leaves → intermediates →
root), but `_compute_root` computes and discards them. `merkle_layers` exposes them **using the
exact same pairing/padding rule** as `_compute_root`, so a rendered tree matches the sealed root
byte-for-byte. This is a pure, read-only addition — it touches no signing or verification path.

The load-bearing test is :func:`test_layers_root_matches_compute_root`: for every tree size, the
last layer must equal ``[_compute_root(leaves)]``. If the two ever diverge, this fails.
"""
from __future__ import annotations

import pytest

from novafabric.trust.novaseal.merkle import (
    MerkleError,
    _compute_root,
    _leaf_hash,
    _node_hash,
    merkle_layers,
)


def _leaves(n: int) -> list[str]:
    return [_leaf_hash(f"entry-{i}".encode()) for i in range(n)]


@pytest.mark.parametrize("n", [1, 2, 3, 4, 5, 7, 8, 16])
def test_layers_root_matches_compute_root(n):
    leaves = _leaves(n)
    layers = merkle_layers(leaves)
    assert layers[-1] == [_compute_root(leaves)]


def test_first_layer_is_the_unpadded_leaves():
    leaves = _leaves(3)
    assert merkle_layers(leaves)[0] == leaves


def test_two_leaves_intermediate_uses_node_hash():
    a, b = _leaves(2)
    layers = merkle_layers([a, b])
    assert layers == [[a, b], [_node_hash(a, b)]]


def test_odd_layer_pads_with_duplicate():
    a, b, c = _leaves(3)
    layers = merkle_layers([a, b, c])
    # level 1: pair (a,b) and pad c with itself → (c,c)
    assert layers[1] == [_node_hash(a, b), _node_hash(c, c)]
    # root pairs the two level-1 nodes
    assert layers[2] == [_node_hash(_node_hash(a, b), _node_hash(c, c))]


def test_single_leaf_is_its_own_root():
    leaves = _leaves(1)
    assert merkle_layers(leaves) == [leaves]


def test_empty_tree_raises():
    with pytest.raises(MerkleError):
        merkle_layers([])


def test_input_is_not_mutated():
    leaves = _leaves(3)
    snapshot = list(leaves)
    merkle_layers(leaves)
    assert leaves == snapshot
