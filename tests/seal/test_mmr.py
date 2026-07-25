# Copyright 2024 NovaFabric Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""ADR-0110 §NF-051 (D14) — the append-only Merkle Mountain Range append log.

An MMR is an append-optimized accumulator: O(log n) persistent state (the peaks),
O(log n) inclusion proofs, and an append-only root. This exercises the core
invariant — **every appended leaf is provably included in the current root, and no
tampered proof verifies** — across many sizes.
"""

from __future__ import annotations

import pytest

from novafabric.trust.novaseal.mmr import (
    MerkleMountainRange,
    leaf_hash,
    verify_mmr_proof,
)


def _mmr(n: int) -> MerkleMountainRange:
    m = MerkleMountainRange()
    for i in range(n):
        m.append(leaf_hash(f"event-{i}".encode()))
    return m


class TestAppendAndRoot:
    def test_empty_root_is_none(self) -> None:
        assert MerkleMountainRange().root() is None

    def test_single_leaf_root_is_the_leaf(self) -> None:
        m = MerkleMountainRange()
        lh = leaf_hash(b"only")
        m.append(lh)
        assert m.root() == lh
        assert m.size == 1

    def test_root_changes_on_append(self) -> None:
        m = _mmr(3)
        r3 = m.root()
        m.append(leaf_hash(b"event-3"))
        assert m.root() != r3
        assert m.size == 4

    def test_deterministic(self) -> None:
        assert _mmr(7).root() == _mmr(7).root()


class TestInclusionProofs:
    @pytest.mark.parametrize("n", [1, 2, 3, 4, 5, 8, 11, 16, 21])
    def test_every_leaf_proof_verifies(self, n: int) -> None:
        m = _mmr(n)
        root = m.root()
        assert root is not None
        for i in range(n):
            lh = leaf_hash(f"event-{i}".encode())
            proof = m.inclusion_proof(i)
            assert verify_mmr_proof(lh, proof, root) is True

    def test_wrong_leaf_is_rejected(self) -> None:
        m = _mmr(8)
        root = m.root()
        proof = m.inclusion_proof(3)
        assert verify_mmr_proof(leaf_hash(b"not-the-leaf"), proof, root) is False

    def test_tampered_proof_path_is_rejected(self) -> None:
        m = _mmr(8)
        root = m.root()
        proof = m.inclusion_proof(3)
        if proof.path:
            side, sib = proof.path[0]
            proof.path[0] = (side, "deadbeef" * 8)  # corrupt a sibling hash
        assert verify_mmr_proof(leaf_hash(b"event-3"), proof, root) is False

    def test_wrong_root_is_rejected(self) -> None:
        m = _mmr(8)
        proof = m.inclusion_proof(3)
        assert verify_mmr_proof(leaf_hash(b"event-3"), proof, "00" * 32) is False

    def test_out_of_range_index_raises(self) -> None:
        m = _mmr(4)
        with pytest.raises(IndexError):
            m.inclusion_proof(4)


class TestAppendOnly:
    def test_old_leaf_still_proves_after_more_appends(self) -> None:
        m = _mmr(4)
        lh0 = leaf_hash(b"event-0")
        for i in range(4, 20):
            m.append(leaf_hash(f"event-{i}".encode()))
        # Proof for leaf 0 against the *new* root still verifies (append-only).
        proof = m.inclusion_proof(0)
        assert verify_mmr_proof(lh0, proof, m.root()) is True
