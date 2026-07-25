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
"""Merkle Mountain Range append-only log (ADR-0110 §NF-051, D14).

An MMR is an append-optimized accumulator: its persistent state is the small set
of **peaks** (the roots of the perfect binary subtrees that tile the leaves), so
state and inclusion proofs are both O(log n). The root is the deterministic *bag
of peaks* — folding the peaks right-to-left under a hash. Appending never rewrites
an existing node, so an old leaf's inclusion proof still verifies against every
later root: the log is provably append-only.

This is the verifiable-half foundation of NF-051's cross-node interaction proofs —
a node's *signed ordering commitment* is a signature over an MMR root of its
worker-capsule event log. The cross-node happened-before consolidation over Slurm
(the capture half) is a documented later slice.

Domain separation follows the transparency-log convention: leaves are hashed with
a ``0x00`` prefix and internal nodes with ``0x01``.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field


def leaf_hash(data: bytes) -> str:
    """Hash a leaf payload with the leaf domain prefix (0x00); return hex."""
    return hashlib.sha256(b"\x00" + data).hexdigest()


def _node_hash(left_hex: str, right_hex: str) -> str:
    """Hash two child hex hashes with the internal-node domain prefix (0x01)."""
    return hashlib.sha256(b"\x01" + bytes.fromhex(left_hex) + bytes.fromhex(right_hex)).hexdigest()


@dataclass
class MMRProof:
    """An inclusion proof for one leaf: the path to its peak plus the sibling peaks."""

    leaf_index: int
    size: int
    #: (side, sibling_hash) from leaf up to its subtree peak; side is 'L' if the
    #: sibling is on the left (so ``H(sibling, acc)``), 'R' if on the right.
    path: list[tuple[str, str]]
    #: Hashes of the peaks to the left of this leaf's peak (bagging order).
    peaks_before: list[str]
    #: Hashes of the peaks to the right of this leaf's peak (bagging order).
    peaks_after: list[str]


def _bag_peaks(peaks: list[str]) -> str:
    """Fold peaks right-to-left: root = H(p0, H(p1, ... H(p_{n-2}, p_{n-1}))))."""
    acc = peaks[-1]
    for peak in reversed(peaks[:-1]):
        acc = _node_hash(peak, acc)
    return acc


@dataclass
class MerkleMountainRange:
    """An append-only MMR over leaf hashes (hex strings)."""

    _nodes: list[str] = field(default_factory=list)  # every node, in creation order
    _parent: dict[int, int] = field(default_factory=dict)
    _sibling: dict[int, tuple[str, int]] = field(default_factory=dict)  # idx -> (side, sib_idx)
    _leaf_nodes: list[int] = field(default_factory=list)  # leaf-index -> node index
    # Peak stack: (height, node_index), strictly decreasing height left→right.
    _peaks: list[tuple[int, int]] = field(default_factory=list)

    @property
    def size(self) -> int:
        """Number of leaves appended."""
        return len(self._leaf_nodes)

    def append(self, leaf_hex: str) -> int:
        """Append a leaf hash; return its 0-based leaf index."""
        idx = len(self._nodes)
        self._nodes.append(leaf_hex)
        self._leaf_nodes.append(idx)
        self._peaks.append((0, idx))
        # Merge equal-height top peaks into parents (the MMR carry).
        while len(self._peaks) >= 2 and self._peaks[-1][0] == self._peaks[-2][0]:
            height, right_idx = self._peaks.pop()
            _, left_idx = self._peaks.pop()
            parent_idx = len(self._nodes)
            self._nodes.append(_node_hash(self._nodes[left_idx], self._nodes[right_idx]))
            self._parent[left_idx] = parent_idx
            self._parent[right_idx] = parent_idx
            self._sibling[left_idx] = ("R", right_idx)  # left's sibling is on its right
            self._sibling[right_idx] = ("L", left_idx)  # right's sibling is on its left
            self._peaks.append((height + 1, parent_idx))
        return self.size - 1

    def root(self) -> str | None:
        """Return the current MMR root (bag of peaks), or None if empty."""
        if not self._peaks:
            return None
        return _bag_peaks([self._nodes[i] for i in (idx for _, idx in self._peaks)])

    def inclusion_proof(self, leaf_index: int) -> MMRProof:
        """Build an O(log n) inclusion proof for the leaf at *leaf_index*."""
        if leaf_index < 0 or leaf_index >= self.size:
            raise IndexError(f"leaf_index {leaf_index} out of range (size {self.size})")
        node = self._leaf_nodes[leaf_index]
        path: list[tuple[str, str]] = []
        while node in self._parent:
            side, sib_idx = self._sibling[node]
            path.append((side, self._nodes[sib_idx]))
            node = self._parent[node]
        # ``node`` is now this leaf's peak. Split the peak list around it.
        peak_indices = [idx for _, idx in self._peaks]
        j = peak_indices.index(node)
        return MMRProof(
            leaf_index=leaf_index,
            size=self.size,
            path=path,
            peaks_before=[self._nodes[i] for i in peak_indices[:j]],
            peaks_after=[self._nodes[i] for i in peak_indices[j + 1:]],
        )


def verify_mmr_proof(leaf_hex: str, proof: MMRProof, root: str | None) -> bool:
    """Verify *leaf_hex* is included in an MMR with *root*, given *proof*.

    Pure function — recomputes the leaf's subtree peak from the path, re-bags it
    with the sibling peaks, and compares to *root*. Returns False on any mismatch
    or malformed proof rather than raising.
    """
    if root is None:
        return False
    try:
        acc = leaf_hex
        for side, sibling in proof.path:
            if side == "L":
                acc = _node_hash(sibling, acc)
            elif side == "R":
                acc = _node_hash(acc, sibling)
            else:
                return False
        peaks = list(proof.peaks_before) + [acc] + list(proof.peaks_after)
        return _bag_peaks(peaks) == root
    except (ValueError, TypeError):
        return False


__all__ = [
    "MMRProof",
    "MerkleMountainRange",
    "leaf_hash",
    "verify_mmr_proof",
]
