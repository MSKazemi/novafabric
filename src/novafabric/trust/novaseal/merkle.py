"""Merkle log backends for NovaSeal v0.1 — SQLite (default) and Postgres (Scale-S4).

Per ADR-003 (regulated-industries study): each capsule signing event appends
a leaf to the tenant's Merkle tree. The root is recomputed on each append.

Tables:
    leaves(leaf_index INTEGER PK, leaf_hash TEXT, entry_json TEXT)
    tree_heads(tree_size INTEGER PK, root_hash TEXT, root_version INTEGER NULL)
    log_meta(key TEXT PK, value TEXT)   -- holds the log's ``root_version``

Append-only invariant is enforced at the DB level (SQLite triggers /
Postgres rules + application-layer guard on Postgres).

Merkle tree structure — the root is versioned (ADR-0313):
    Both versions hash leaves as H(0x00 || entry_bytes) and internal nodes as
    H(0x01 || left || right); the empty tree's root is H(b"").

    - **v2** (``ROOT_VERSION_V2``, written by every log created from
      ADR-0313 on): the RFC 6962 / RFC 9162 Merkle Tree Hash. An n-leaf tree
      splits at the largest power of two k < n; equivalently, an odd node at
      the end of a level is promoted to the next level unchanged. Nothing is
      duplicated, so the root commits to the tree size: [a, b, c] and
      [a, b, c, c] have different roots. Inclusion and consistency proofs are
      the RFC 9162 §2.1.3 / §2.1.4 audit and consistency paths.
    - **v1** (``ROOT_VERSION_V1``, every log and seal written before ADR-0313):
      an odd level is padded by duplicating its last node. The root does
      **not** commit to the tree size — [a, b, c] and [a, b, c, c] share a
      root — so a v1 root is verified only together with its ``tree_size``
      (:func:`verify_root`, :func:`verify_consistency_proof`, and the
      ``nova verify`` log check all refuse a v1 root presented without one).

    A log has exactly one root version for its whole life, recorded in
    ``log_meta`` and on every tree head; a structure that carries a root
    (log entry, checkpoint, consistency proof) carries ``root_version`` beside
    it, and an absent ``root_version`` means v1. The two versions coincide
    for power-of-two sizes (the trees are identical there).

Inclusion proof:
    A list of sibling hashes, leaf to root, needed to reconstruct the root
    from a given leaf. The verifier recomputes upward from the leaf and needs
    the tree size (v1: constant proof length per size; v2: the RFC 9162
    algorithm consumes exactly the path the size implies).

Scale-S4 — Postgres backend
    ``PostgresMerkleLog`` uses psycopg (psycopg3).  Install:
        pip install novafabric[seal-postgres]

    ``verify_consistency()`` on the Postgres backend is *partially* sampled:
    it spot-checks SAMPLE_SIZE random leaves, then verifies the current root
    against a root recomputed from **all** stored leaf hashes.  Sampling
    therefore applies only to the entry_json re-hash (entries outside the
    sample are trusted to match their stored leaf_hash, which is sound given
    the append-only guarantee enforced at the DB layer).  The root
    recomputation is never sampled — that full pass *is* the tamper
    detection for the stored leaf_hash column.

    **Full recompute is the guarantee; its linear cost is documented, not
    optimised away (ADR-0268, owner decision 2026-10-09).**  Recomputing
    the root over 1M leaves costs ~1M SHA-256 calls in Python: measured at
    1361 ms in-process with no database involved at all, plus the cost of
    streaming 1M hashes out of Postgres (~1879 ms end-to-end on a CI
    runner).  The former Scale-S4 criterion of p99 < 200 ms at 1M entries
    was never met and is withdrawn, not deferred.

    Reaching 200 ms would mean not recomputing the full root — verifying
    inclusion proofs for the sampled leaves against a signed tree head
    instead, as Certificate Transparency does.  That is a different and
    weaker guarantee, not a speed-up, and ADR-0268 rejects it for now.  The
    nightly ``tests/seal/test_postgres_merkle.py`` benchmark is a regression
    guard on this cost, not a latency target.

    For a full re-hash audit (slower, O(N) Python SHA-256 calls), call
    ``verify_consistency(full=True)``.

Factory:
    Use ``open_merkle_log(uri)`` to obtain the right backend automatically:
    - ``Path`` / ``str`` path → ``MerkleLog`` (SQLite)
    - ``str`` starting with ``postgresql://``, ``postgres://`` or
      ``postgresql+psycopg://`` → ``PostgresMerkleLog``
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Union

from novafabric._sqlite_util import connect_sqlite, ensure_wal

#: Legacy root (through v0.105): odd levels padded by duplicating the last node.
#: Does not commit to the tree size; verified only with ``tree_size`` (ADR-0313).
ROOT_VERSION_V1 = 1
#: RFC 6962 / RFC 9162 Merkle Tree Hash; commits to the tree size (ADR-0313).
ROOT_VERSION_V2 = 2
#: The version every newly created log writes.
CURRENT_ROOT_VERSION = ROOT_VERSION_V2
SUPPORTED_ROOT_VERSIONS = frozenset({ROOT_VERSION_V1, ROOT_VERSION_V2})

#: ``scheme`` of a v1 consistency proof (aligned perfect-subtree decomposition).
CONSISTENCY_SCHEME_V1 = "novafabric-padded-subtree-v1"
#: ``scheme`` of a v2 consistency proof (RFC 9162 §2.1.4 consistency path).
CONSISTENCY_SCHEME_V2 = "rfc9162-consistency"


class MerkleError(Exception):
    """Raised on Merkle log integrity violations."""


class MerkleSizeRequiredError(MerkleError):
    """A root or proof was presented for verification without its tree size.

    A v1 root does not commit to the tree size, so it means nothing without
    one; ADR-0313 applies the same rule to v2 so no verification path depends
    on which version it happens to be handed.
    """


class UnsupportedRootVersionError(MerkleError):
    """A ``root_version`` that this NovaFabric does not implement."""


@dataclass
class ConsistencyResult:
    """Result of a Merkle log consistency verification."""

    leaf_count: int
    root_hash: str
    consistent: bool
    errors: list[str] = field(default_factory=list)
    # Additive (ADR-0313): the root version the log was verified under.
    root_version: int = ROOT_VERSION_V1


def root_version_of(record: Mapping[str, Any]) -> int:
    """Return the ``root_version`` a record declares — absent (or null) means v1.

    Raises:
        UnsupportedRootVersionError: for a value that is not a supported version.
    """
    value = record.get("root_version")
    if value is None:
        return ROOT_VERSION_V1
    return _check_root_version(value)


def _check_root_version(value: object) -> int:
    supported = isinstance(value, int) and not isinstance(value, bool)
    if not supported or value not in SUPPORTED_ROOT_VERSIONS:
        raise UnsupportedRootVersionError(
            f"unsupported root_version {value!r} (supported: "
            f"{sorted(SUPPORTED_ROOT_VERSIONS)}, ADR-0313)"
        )
    return value


def _is_size(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


# ---------------------------------------------------------------------------
# Hash primitives
# ---------------------------------------------------------------------------

def _leaf_hash(entry_bytes: bytes) -> str:
    return hashlib.sha256(b"\x00" + entry_bytes).hexdigest()


def _node_hash(left: str, right: str) -> str:
    left_b = bytes.fromhex(left)
    right_b = bytes.fromhex(right)
    return hashlib.sha256(b"\x01" + left_b + right_b).hexdigest()


def _empty_hash() -> str:
    return hashlib.sha256(b"").hexdigest()


# ---------------------------------------------------------------------------
# Merkle root computation
# ---------------------------------------------------------------------------

def _compute_root(leaf_hashes: list[str]) -> str:
    """Compute the **v1** Merkle root (odd level padded by duplicating its last node).

    Kept so that every log and seal written before ADR-0313 keeps verifying. The v1
    root does **not** commit to the tree size — ``[a, b, c]`` and ``[a, b, c, c]``
    have the same root — so it must only ever be compared together with its
    ``tree_size``. New code should call :func:`compute_root` with an explicit version.
    """
    if not leaf_hashes:
        return _empty_hash()
    nodes = list(leaf_hashes)
    while len(nodes) > 1:
        if len(nodes) % 2 == 1:
            nodes.append(nodes[-1])  # pad with duplicate
        nodes = [_node_hash(nodes[i], nodes[i + 1]) for i in range(0, len(nodes), 2)]
    return nodes[0]


def _next_level_v2(nodes: list[str]) -> list[str]:
    """One RFC 6962 level up: pair left to right, promote a trailing odd node unchanged."""
    parents = [_node_hash(nodes[i], nodes[i + 1]) for i in range(0, len(nodes) - 1, 2)]
    if len(nodes) % 2 == 1:
        parents.append(nodes[-1])
    return parents


def _compute_root_v2(leaf_hashes: list[str]) -> str:
    """The **v2** root: RFC 6962 MTH — split at the largest power of two, no duplication.

    Computed level by level with odd-node promotion, which is the same tree as the
    recursive RFC 6962 §2.1 definition. Commits to the tree size.
    """
    if not leaf_hashes:
        return _empty_hash()
    nodes = list(leaf_hashes)
    while len(nodes) > 1:
        nodes = _next_level_v2(nodes)
    return nodes[0]


def compute_root(leaf_hashes: list[str], *, root_version: int) -> str:
    """Compute the Merkle root of *leaf_hashes* under an explicit *root_version*."""
    if _check_root_version(root_version) == ROOT_VERSION_V2:
        return _compute_root_v2(leaf_hashes)
    return _compute_root(leaf_hashes)


def verify_root(
    leaf_hashes: list[str],
    root_hash: str,
    *,
    tree_size: int | None,
    root_version: int | None,
) -> bool:
    """Check that *leaf_hashes* are exactly a tree of *tree_size* leaves with *root_hash*.

    Both the size and the root are compared, for both versions (ADR-0313).
    *root_version* ``None`` means v1, as for a record without the field.

    Raises:
        MerkleSizeRequiredError: when *tree_size* is ``None`` — a root is never
            verified without its size.
        UnsupportedRootVersionError: for an unknown *root_version*.
    """
    if tree_size is None:
        raise MerkleSizeRequiredError(
            "a Merkle root is verified only together with its tree_size (ADR-0313)"
        )
    version = ROOT_VERSION_V1 if root_version is None else _check_root_version(root_version)
    if not _is_size(tree_size) or tree_size != len(leaf_hashes):
        return False
    return compute_root(leaf_hashes, root_version=version) == root_hash


def merkle_layers(
    leaf_hashes: list[str], *, root_version: int = ROOT_VERSION_V1
) -> list[list[str]]:
    """Return every layer of the Merkle tree, leaves first and the root last.

    Read-only companion to :func:`compute_root` for visualization (ADR-0172). It applies
    the pairing rule of *root_version* — v1 (the default, as for a document without a
    ``root_version``) duplicates the last node of an odd layer before pairing; v2 promotes
    it to the next layer unchanged — so ``merkle_layers(x, root_version=v)[-1] ==
    [compute_root(x, root_version=v)]`` always holds. The first layer is the leaves exactly
    as given (never padded); each subsequent layer is the level above it, as stored (a v1
    layer never shows its padding duplicate). The input list is not mutated. This touches
    no signing or verification path — it only re-derives the hashes the root computation
    discards.
    """
    if not leaf_hashes:
        raise MerkleError("Empty tree has no layers")
    version = _check_root_version(root_version)
    layers: list[list[str]] = [list(leaf_hashes)]
    nodes = list(leaf_hashes)
    while len(nodes) > 1:
        if version == ROOT_VERSION_V2:
            nodes = _next_level_v2(nodes)
        else:
            if len(nodes) % 2 == 1:
                nodes = [*nodes, nodes[-1]]  # pad with duplicate (new list; never mutate a layer)
            nodes = [_node_hash(nodes[i], nodes[i + 1]) for i in range(0, len(nodes), 2)]
        layers.append(list(nodes))
    return layers


# ---------------------------------------------------------------------------
# Inclusion proof
# ---------------------------------------------------------------------------

def _compute_inclusion_proof(
    leaf_index: int, leaf_hashes: list[str], root_version: int = ROOT_VERSION_V1
) -> list[str]:
    """Return sibling hashes (leaf to root) needed to prove leaf_index is in the tree.

    v1 (the default, for pre-ADR-0313 callers): one sibling per level, the padded
    duplicate included. v2: the RFC 9162 §2.1.3.1 audit path — a level where the
    node is promoted unchanged contributes no sibling.
    """
    if not leaf_hashes:
        raise MerkleError("Empty tree has no inclusion proof")
    if leaf_index < 0 or leaf_index >= len(leaf_hashes):
        raise MerkleError(
            f"leaf_index {leaf_index} out of range (tree size {len(leaf_hashes)})"
        )
    version = _check_root_version(root_version)

    nodes = list(leaf_hashes)
    idx = leaf_index
    proof = []

    while len(nodes) > 1:
        if version == ROOT_VERSION_V2:
            sibling = idx ^ 1
            if sibling < len(nodes):  # else: the lone trailing node, promoted as is
                proof.append(nodes[sibling])
            nodes = _next_level_v2(nodes)
        else:
            if len(nodes) % 2 == 1:
                nodes.append(nodes[-1])
            sibling = idx ^ 1  # XOR with 1 flips the last bit: left↔right
            proof.append(nodes[sibling])
            nodes = [_node_hash(nodes[i], nodes[i + 1]) for i in range(0, len(nodes), 2)]
        idx //= 2

    return proof


def _verify_inclusion_v2(
    leaf_hash: str, leaf_index: int, proof: list[str], expected_root: str, tree_size: int
) -> bool:
    """RFC 9162 §2.1.3.2 inclusion verification (consumes exactly the path *tree_size* implies)."""
    fn = leaf_index
    sn = tree_size - 1
    current = leaf_hash
    for sibling in proof:
        if sn == 0:
            return False  # path longer than the tree of this size allows
        if fn & 1 or fn == sn:
            current = _node_hash(sibling, current)
            if not fn & 1:
                while not fn & 1 and fn != 0:
                    fn >>= 1
                    sn >>= 1
        else:
            current = _node_hash(current, sibling)
        fn >>= 1
        sn >>= 1
    return sn == 0 and current == expected_root


def verify_inclusion_proof(
    leaf_hash: str,
    leaf_index: int,
    proof: list[str],
    expected_root: str,
    tree_size: int,
    *,
    root_version: int = ROOT_VERSION_V1,
) -> bool:
    """Recompute the root from *proof* and compare it to *expected_root* at *tree_size*.

    *root_version* defaults to v1 — the meaning of a record with no ``root_version``.

    Rejects out-of-range leaf indices: because v1 odd levels are padded by
    duplicating the last leaf, a v1 proof for an index >= tree_size could
    otherwise recompute to the real root (a phantom-inclusion soundness gap).
    A v1 proof cannot tell ``tree_size`` n from n + 1 when leaf n duplicates
    leaf n - 1; only the log can (see ``nova verify``'s local-log check). A v2
    proof is checked with the RFC 9162 algorithm, which binds it to *tree_size*.
    """
    version = _check_root_version(root_version)
    if not _is_size(tree_size) or tree_size <= 0 or leaf_index < 0 or leaf_index >= tree_size:
        return False
    if version == ROOT_VERSION_V2:
        return _verify_inclusion_v2(leaf_hash, leaf_index, proof, expected_root, tree_size)

    idx = leaf_index
    current = leaf_hash

    for sibling in proof:
        if idx % 2 == 0:
            current = _node_hash(current, sibling)
        else:
            current = _node_hash(sibling, current)
        idx //= 2

    return current == expected_root


def inclusion_proof_length(tree_size: int) -> int:
    """Number of sibling hashes in a **v1** inclusion proof for a tree of *tree_size* leaves.

    Under the v1 duplicate-padding construction every level halves (rounding up) until
    one node remains, so every leaf's proof has exactly this many entries. A carried
    v1 proof of any other length is malformed. (A v2 path length depends on the leaf
    index; :func:`verify_inclusion_proof` enforces it for v2.)
    """
    if tree_size <= 0:
        raise MerkleError("tree_size must be positive")
    depth = 0
    nodes = tree_size
    while nodes > 1:
        nodes = (nodes + 1) // 2
        depth += 1
    return depth


# ---------------------------------------------------------------------------
# Consistency proof (ADR-0041 v0.2, gap-001; versioned by ADR-0313)
#
# v1: the tree pads odd levels by duplicating the last node, which differs
# from RFC 6962 §2.1 unbalanced trees — so RFC 6962 consistency proofs do not
# apply verbatim (for non-power-of-two sizes the old root is not a node of
# the new tree).  The v1 proof is therefore an *aligned perfect-subtree
# decomposition* proof, valid against this duplicate-padding tree shape:
#
#   proof = decomposition of leaves[0:old_size] into maximal aligned perfect
#           subtrees (their root hashes), plus the decomposition of
#           leaves[old_size:new_size].
#
# The verifier recomputes BOTH roots from those O(log n + log m) hashes using
# the same padding rule; because the old-prefix commitment appears in both
# computations, matching roots prove the new log extends the old one
# append-only.  ADR-0313 adds the check that the subtree heights are exactly
# the decomposition the claimed old_size / new_size imply — without it, a v1
# proof said nothing about the sizes it connected.
#
# v2: the RFC 9162 §2.1.4 consistency path (``scheme`` CONSISTENCY_SCHEME_V2),
# wire-compatible in content with RFC 6962 / RFC 9162 consistency proofs.
# ---------------------------------------------------------------------------


def _perfect_root(leaf_hashes: list[str]) -> str:
    """Root of a perfect (power-of-two) subtree — pure pairing, no padding."""
    nodes = list(leaf_hashes)
    while len(nodes) > 1:
        nodes = [_node_hash(nodes[i], nodes[i + 1]) for i in range(0, len(nodes), 2)]
    return nodes[0]


def _aligned_blocks(start: int, end: int) -> list[tuple[int, int]]:
    """``(offset, size)`` of the maximal aligned perfect subtrees covering [start, end)."""
    blocks: list[tuple[int, int]] = []
    i = start
    while i < end:
        size = 1
        while i % (size * 2) == 0 and i + size * 2 <= end:
            size *= 2
        blocks.append((i, size))
        i += size
    return blocks


def _decompose_range(
    leaf_hashes: list[str], start: int, end: int
) -> list[dict[str, Any]]:
    """Decompose leaves[start:end] into maximal aligned perfect subtrees.

    Each part is ``{"height": h, "hash": root}`` for a subtree of 2**h leaves
    whose global start index is a multiple of its size.
    """
    return [
        {"height": size.bit_length() - 1, "hash": _perfect_root(leaf_hashes[i : i + size])}
        for i, size in _aligned_blocks(start, end)
    ]


def _root_from_parts(parts: list[dict[str, Any]]) -> str:
    """Recompute the v1 duplicate-padding tree root from aligned subtree parts.

    First merges adjacent equal-height parts bottom-up (binary-counter
    stack), then folds right-to-left raising the right accumulator with
    ``H(r, r)`` duplication — exactly what :func:`_compute_root` does to a
    trailing partial subtree.
    """
    if not parts:
        return _empty_hash()
    stack: list[tuple[int, str]] = []
    for part in parts:
        height, digest = int(part["height"]), str(part["hash"])
        stack.append((height, digest))
        while len(stack) >= 2 and stack[-1][0] == stack[-2][0]:
            h_right, right = stack.pop()
            _h_left, left = stack.pop()
            stack.append((h_right + 1, _node_hash(left, right)))
    height, current = stack.pop()
    while stack:
        target_height, left = stack.pop()
        while height < target_height:
            current = _node_hash(current, current)
            height += 1
        current = _node_hash(left, current)
        height += 1
    return current


def _verify_consistency_v1(
    proof: Mapping[str, Any], old_root: str, new_root: str, old_size: int, new_size: int
) -> bool:
    if proof.get("scheme", CONSISTENCY_SCHEME_V1) != CONSISTENCY_SCHEME_V1:
        return False
    old_parts = list(proof.get("old_parts", []))
    tail_parts = list(proof.get("tail_parts", []))
    if not old_parts:
        return False
    # ADR-0313: the parts must be exactly the decomposition the claimed sizes imply.
    expected_old = [size.bit_length() - 1 for _i, size in _aligned_blocks(0, old_size)]
    expected_tail = [size.bit_length() - 1 for _i, size in _aligned_blocks(old_size, new_size)]
    if [int(p["height"]) for p in old_parts] != expected_old:
        return False
    if [int(p["height"]) for p in tail_parts] != expected_tail:
        return False
    if _root_from_parts(old_parts) != old_root:
        return False
    return _root_from_parts(old_parts + tail_parts) == new_root


def _largest_power_of_two_below(n: int) -> int:
    """Largest power of two strictly less than *n* (n >= 2) — the RFC 6962 split point."""
    k = 1
    while k * 2 < n:
        k *= 2
    return k


def _subproof_v2(m: int, leaves: list[str], complete: bool) -> list[str]:
    """RFC 6962 §2.1.2 SUBPROOF(m, D[n], b)."""
    n = len(leaves)
    if m == n:
        return [] if complete else [_compute_root_v2(leaves)]
    k = _largest_power_of_two_below(n)
    if m <= k:
        return [*_subproof_v2(m, leaves[:k], complete), _compute_root_v2(leaves[k:])]
    return [*_subproof_v2(m - k, leaves[k:], False), _compute_root_v2(leaves[:k])]


def _verify_consistency_v2(
    old_size: int, new_size: int, path: list[str], old_root: str, new_root: str
) -> bool:
    """RFC 9162 §2.1.4.2 consistency verification."""
    if old_size == new_size:
        return not path and old_root == new_root
    if not path:
        return False
    if old_size & (old_size - 1) == 0:  # an exact power of two: the old root is a node
        path = [old_root, *path]
    fn = old_size - 1
    sn = new_size - 1
    while fn & 1:
        fn >>= 1
        sn >>= 1
    first = second = path[0]
    for node in path[1:]:
        if sn == 0:
            return False
        if fn & 1 or fn == sn:
            first = _node_hash(node, first)
            second = _node_hash(node, second)
            if not fn & 1:
                while not fn & 1 and fn != 0:
                    fn >>= 1
                    sn >>= 1
        else:
            second = _node_hash(second, node)
        fn >>= 1
        sn >>= 1
    return sn == 0 and first == old_root and second == new_root


def verify_consistency_proof(
    proof: Mapping[str, Any],
    old_root: str,
    new_root: str,
    *,
    old_size: int | None = None,
    new_size: int | None = None,
) -> bool:
    """Offline check that the log at *new_size* extends the log at *old_size*.

    *proof* is the dict produced by ``MerkleLog.consistency_proof()`` /
    ``PostgresMerkleLog.consistency_proof()``; its ``root_version`` (absent: v1)
    selects the algorithm. Pure function — no log access.

    ADR-0313: the sizes are required. Each root is compared together with the
    size it belongs to: the proof's own ``old_size`` / ``new_size`` must equal the
    caller's, and the proof's structure must be the one those sizes imply.

    Raises:
        MerkleSizeRequiredError: when *old_size* or *new_size* is not given.
    """
    if old_size is None or new_size is None:
        raise MerkleSizeRequiredError(
            "a consistency proof is verified only against the old_size and new_size "
            "whose roots it connects (ADR-0313)"
        )
    if not _is_size(old_size) or not _is_size(new_size) or not 1 <= old_size <= new_size:
        return False
    for key, expected in (("old_size", old_size), ("new_size", new_size)):
        if key in proof and proof[key] != expected:
            return False
    try:
        version = root_version_of(proof)
    except UnsupportedRootVersionError:
        return False
    try:
        if version == ROOT_VERSION_V1:
            return _verify_consistency_v1(proof, old_root, new_root, old_size, new_size)
        path = proof.get("path")
        if proof.get("scheme") != CONSISTENCY_SCHEME_V2 or not isinstance(path, list):
            return False
        if not all(isinstance(h, str) for h in path):
            return False
        return _verify_consistency_v2(old_size, new_size, path, old_root, new_root)
    except (KeyError, TypeError, ValueError):  # malformed part or non-hex hash
        return False


def _build_consistency_proof(
    leaf_hashes: list[str],
    old_size: int,
    new_size: int,
    root_version: int = ROOT_VERSION_V1,
) -> dict[str, Any]:
    """Shared proof builder over an in-memory leaf-hash list."""
    if old_size <= 0:
        raise MerkleError("old_size must be >= 1")
    if old_size > new_size:
        raise MerkleError(
            f"old_size {old_size} exceeds new_size {new_size}"
        )
    if new_size > len(leaf_hashes):
        raise MerkleError(
            f"new_size {new_size} exceeds tree size {len(leaf_hashes)}"
        )
    version = _check_root_version(root_version)
    if version == ROOT_VERSION_V2:
        return {
            "scheme": CONSISTENCY_SCHEME_V2,
            "root_version": ROOT_VERSION_V2,
            "old_size": old_size,
            "new_size": new_size,
            "path": _subproof_v2(old_size, leaf_hashes[:new_size], True),
            "old_root": _compute_root_v2(leaf_hashes[:old_size]),
            "new_root": _compute_root_v2(leaf_hashes[:new_size]),
        }
    return {
        "scheme": CONSISTENCY_SCHEME_V1,
        "root_version": ROOT_VERSION_V1,
        "old_size": old_size,
        "new_size": new_size,
        "old_parts": _decompose_range(leaf_hashes, 0, old_size),
        "tail_parts": _decompose_range(leaf_hashes, old_size, new_size),
        "old_root": _root_from_parts(_decompose_range(leaf_hashes, 0, old_size)),
        "new_root": _compute_root(leaf_hashes[:new_size]),
    }


# ---------------------------------------------------------------------------
# MerkleLog — SQLite-backed, append-only
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS leaves (
    leaf_index  INTEGER PRIMARY KEY,
    leaf_hash   TEXT    NOT NULL,
    entry_json  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS tree_heads (
    tree_size   INTEGER PRIMARY KEY,
    root_hash   TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS log_meta (
    key         TEXT    PRIMARY KEY,
    value       TEXT    NOT NULL
);

CREATE TRIGGER IF NOT EXISTS reject_leaf_delete
BEFORE DELETE ON leaves
BEGIN
    SELECT RAISE(FAIL, 'leaves is append-only; DELETE is forbidden');
END;

CREATE TRIGGER IF NOT EXISTS reject_leaf_update
BEFORE UPDATE ON leaves
BEGIN
    SELECT RAISE(FAIL, 'leaves is append-only; UPDATE is forbidden');
END;
"""

_ROOT_VERSION_KEY = "root_version"

_KEYID_RE = re.compile(r"[0-9a-f]{64}")


def _is_keyid(value: object) -> bool:
    """A seal entry ``keyid``: the lowercase SHA-256 hex of the signer certificate."""
    return isinstance(value, str) and _KEYID_RE.fullmatch(value) is not None


def _keyid_pattern(keyid: str) -> str:
    """LIKE pattern for the canonical ``"keyid":"<hex>"`` member of a seal entry.

    Only called with a validated hex digest, so the pattern holds no wildcard.
    """
    return f'%"keyid":"{keyid}"%'


def _resolve_log_root_version(
    recorded: str | None, has_leaves: bool, requested: int | None, where: str
) -> int:
    """The root version of a log: recorded, else v1 for a legacy non-empty log, else new.

    A log written before ADR-0313 has no recorded version but has leaves — its
    tree heads are v1 roots, so it stays v1 for its whole life (switching would
    leave heads of two versions in one log, and no consistency proof spans them).
    """
    if recorded is not None:
        try:
            version = _check_root_version(int(recorded))
        except ValueError as exc:
            raise UnsupportedRootVersionError(
                f"{where}: unreadable root_version {recorded!r}"
            ) from exc
    elif has_leaves:
        version = ROOT_VERSION_V1
    else:
        version = CURRENT_ROOT_VERSION if requested is None else _check_root_version(requested)
    if requested is not None and requested != version:
        raise MerkleError(
            f"{where} records root_version {version}; it cannot be opened as "
            f"root_version {requested} (a log keeps one root version, ADR-0313)"
        )
    return version


class MerkleLog:
    """Append-only Merkle log backed by SQLite.

    Thread-safety: SQLite in WAL mode is used; callers that share the same
    :class:`MerkleLog` instance must hold an external lock for concurrent
    appends. Single-threaded use (one ``nova capture`` at a time) is safe.

    Root version (ADR-0313): a new, empty log writes v2 roots; a log created
    before ADR-0313 (leaves present, no recorded version) stays v1. Pass
    *root_version* only to require a version — a mismatch with the recorded
    one raises :class:`MerkleError`.
    """

    def __init__(self, db_path: Path, *, root_version: int | None = None) -> None:
        # Expand ``~`` here, at the one place that creates the file: before
        # audit L28 ``MerkleLog(Path("~/x.db"))`` made a literal ``./~``
        # directory in the current working directory.
        db_path = Path(db_path).expanduser()
        self._db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = connect_sqlite(str(db_path), check_same_thread=False)
        # Two captures may open a new log at the same moment. An unconditional
        # ``PRAGMA journal_mode=WAL`` returns SQLITE_BUSY to the loser (the busy
        # timeout does not apply to it), and the check-then-ALTER below raced
        # into "duplicate column name". Both are tolerated now.
        ensure_wal(self._conn, what="NovaSeal Merkle log")
        self._conn.executescript(_SCHEMA)
        with self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            columns = {row[1] for row in self._conn.execute("PRAGMA table_info(tree_heads)")}
            if "root_version" not in columns:
                # Additive migration of a pre-ADR-0313 log: NULL means a v1 head.
                self._conn.execute("ALTER TABLE tree_heads ADD COLUMN root_version INTEGER")
        self._root_version = self._init_root_version(root_version)

    def _init_root_version(self, requested: int | None) -> int:
        with self._conn:
            row = self._conn.execute(
                "SELECT value FROM log_meta WHERE key = ?", (_ROOT_VERSION_KEY,)
            ).fetchone()
            if row is None:
                has_leaves = self._conn.execute("SELECT 1 FROM leaves LIMIT 1").fetchone()
                version = _resolve_log_root_version(
                    None, has_leaves is not None, requested, str(self._db_path)
                )
                self._conn.execute(
                    "INSERT OR IGNORE INTO log_meta (key, value) VALUES (?, ?)",
                    (_ROOT_VERSION_KEY, str(version)),
                )
                row = self._conn.execute(
                    "SELECT value FROM log_meta WHERE key = ?", (_ROOT_VERSION_KEY,)
                ).fetchone()
        return _resolve_log_root_version(
            str(row[0]), True, requested, str(self._db_path)
        )

    @property
    def root_version(self) -> int:
        """The root version every tree head of this log is computed under (ADR-0313)."""
        return self._root_version

    def close(self) -> None:
        self._conn.close()

    def _all_leaf_hashes(self) -> list[str]:
        return [
            r[0]
            for r in self._conn.execute(
                "SELECT leaf_hash FROM leaves ORDER BY leaf_index"
            ).fetchall()
        ]

    # ------------------------------------------------------------------
    # Append
    # ------------------------------------------------------------------

    def append(self, entry: dict[str, object]) -> dict[str, object]:
        """Append an entry to the log and return the log entry dict.

        The returned dict contains:
            leaf_index:      int
            leaf_hash:       str  (hex)
            root_hash:       str  (hex)
            tree_size:       int
            root_version:    int  (ADR-0313; the version root_hash is computed under)
            entry:           dict (the original entry)
            inclusion_proof: list[str] (audit path of leaf_index to root_hash)

        Raises:
            MerkleError: on DB integrity errors.
        """
        entry_bytes = json.dumps(entry, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
        leaf_hash = _leaf_hash(entry_bytes)
        entry_json = entry_bytes.decode("utf-8")
        version = self._root_version

        try:
            with self._conn:
                # BEGIN IMMEDIATE takes the write lock *before* reading the next
                # index. Under the default deferred transaction two processes
                # could both read MAX + 1 and the second INSERT failed with a
                # UNIQUE violation, leaving its capsule unsealed (measured: 10 of
                # 200 appends, 8 processes). The busy timeout queues the waiters.
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._conn.execute(
                    "SELECT COALESCE(MAX(leaf_index) + 1, 0) FROM leaves"
                ).fetchone()
                leaf_index: int = row[0]

                self._conn.execute(
                    "INSERT INTO leaves (leaf_index, leaf_hash, entry_json) VALUES (?, ?, ?)",
                    (leaf_index, leaf_hash, entry_json),
                )

                # Recompute root
                all_hashes = self._all_leaf_hashes()
                root_hash = compute_root(all_hashes, root_version=version)
                tree_size = len(all_hashes)

                self._conn.execute(
                    "INSERT OR REPLACE INTO tree_heads (tree_size, root_hash, root_version)"
                    " VALUES (?, ?, ?)",
                    (tree_size, root_hash, version),
                )
                inclusion_proof = _compute_inclusion_proof(leaf_index, all_hashes, version)
        except sqlite3.Error as exc:
            # The documented contract: database faults are MerkleError, which the
            # seal path handles; a raw sqlite3 error escaped it.
            raise MerkleError(f"Merkle log append failed: {type(exc).__name__}: {exc}") from exc

        return {
            "leaf_index": leaf_index,
            "leaf_hash": leaf_hash,
            "root_hash": root_hash,
            "tree_size": tree_size,
            # Additive (ADR-0313): absent in seals written before it, meaning v1.
            "root_version": version,
            "entry": dict(entry),
            # Additive (v0.103): the audit path to root_hash at tree_size, so a
            # verifier without this log can still check inclusion offline.
            "inclusion_proof": inclusion_proof,
        }

    # ------------------------------------------------------------------
    # Read / proof
    # ------------------------------------------------------------------

    def current_root(self) -> str | None:
        """Return the current Merkle root hash, or None if the log is empty."""
        row = self._conn.execute(
            "SELECT root_hash FROM tree_heads ORDER BY tree_size DESC LIMIT 1"
        ).fetchone()
        return row[0] if row else None

    def tree_size(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) FROM leaves").fetchone()
        return int(row[0])

    def get_leaf(self, leaf_index: int) -> dict[str, object] | None:
        """Return the stored log entry dict for *leaf_index*, or None."""
        row = self._conn.execute(
            "SELECT entry_json FROM leaves WHERE leaf_index = ?", (leaf_index,)
        ).fetchone()
        if row is None:
            return None
        result: dict[str, object] = json.loads(row[0])
        return result

    def get_leaf_hash(self, leaf_index: int) -> str:
        """Return the stored leaf_hash for *leaf_index* (same contract as the Postgres log)."""
        row = self._conn.execute(
            "SELECT leaf_hash FROM leaves WHERE leaf_index = ?", (leaf_index,)
        ).fetchone()
        if row is None:
            raise MerkleError(f"leaf_index {leaf_index} not found")
        return str(row[0])

    def find_leaf_index(self, leaf_hash: str) -> int | None:
        """Return the lowest leaf_index whose leaf_hash is *leaf_hash*, or None.

        A leaf hash can occur more than once: sealing an identical manifest
        twice with the same key appends a byte-identical leaf. To ask whether
        a *specific* position holds a leaf, compare :meth:`get_leaf_hash` at
        that index — never compare this lowest match with a recorded index.
        """
        row = self._conn.execute(
            "SELECT MIN(leaf_index) FROM leaves WHERE leaf_hash = ?", (leaf_hash,)
        ).fetchone()
        return None if row is None or row[0] is None else int(row[0])

    def has_entries_for_keyid(self, keyid: str) -> bool:
        """True when the log holds a seal entry whose ``keyid`` is *keyid*.

        Seal entries are canonical JSON (``{"capsule_id", "has_tsr", "keyid"}``,
        sorted keys, no whitespace), so the ``"keyid":"<hex>"`` member is matched
        exactly. *keyid* must be a SHA-256 hex digest (a certificate
        fingerprint); anything else returns False rather than reaching SQL as a
        pattern.
        """
        if not _is_keyid(keyid):
            return False
        row = self._conn.execute(
            "SELECT 1 FROM leaves WHERE entry_json LIKE ? LIMIT 1",
            (_keyid_pattern(keyid),),
        ).fetchone()
        return row is not None

    def get_inclusion_proof(self, leaf_index: int) -> list[str]:
        """Return the inclusion proof (list of sibling hashes) for *leaf_index*.

        The proof is to the current root, under this log's root version.
        """
        return _compute_inclusion_proof(
            leaf_index, self._all_leaf_hashes(), self._root_version
        )

    def verify_entry(self, leaf_index: int) -> bool:
        """Verify that the stored entry at *leaf_index* is consistent with the root."""
        all_hashes = self._all_leaf_hashes()
        if leaf_index < 0 or leaf_index >= len(all_hashes):
            return False

        root = self.current_root()
        if root is None:
            return False

        proof = _compute_inclusion_proof(leaf_index, all_hashes, self._root_version)
        leaf_hash = all_hashes[leaf_index]
        return verify_inclusion_proof(
            leaf_hash, leaf_index, proof, root, len(all_hashes),
            root_version=self._root_version,
        )

    def compute_root_at_size(
        self, tree_size: int, *, root_version: int | None = None
    ) -> str | None:
        """Recompute the root of the first *tree_size* stored leaves (ADR-0313).

        Unlike :meth:`root_at_size` this trusts no stored head: it re-derives the
        root from the leaf hashes, under *root_version* (default: this log's).
        Returns None when the log holds fewer than *tree_size* leaves.
        """
        version = self._root_version if root_version is None else root_version
        all_hashes = self._all_leaf_hashes()
        if not _is_size(tree_size) or tree_size > len(all_hashes):
            return None
        return compute_root(all_hashes[:tree_size], root_version=version)

    def consistency_proof(
        self, old_size: int, new_size: int | None = None
    ) -> dict[str, Any]:
        """Build a consistency proof from *old_size* to *new_size* (ADR-0041 v0.2).

        Verifiable offline with :func:`verify_consistency_proof` (which needs both
        sizes); the proof's ``root_version`` is this log's (ADR-0313). See the
        module-level note on the v1 deviation from RFC 6962 §2.1.
        """
        all_hashes = self._all_leaf_hashes()
        return _build_consistency_proof(
            all_hashes,
            old_size,
            new_size if new_size is not None else len(all_hashes),
            self._root_version,
        )

    def root_at_size(self, tree_size: int) -> str | None:
        """Stored tree head at *tree_size*, if recorded."""
        row = self._conn.execute(
            "SELECT root_hash FROM tree_heads WHERE tree_size = ?", (tree_size,)
        ).fetchone()
        return row[0] if row else None

    def verify_consistency(self) -> ConsistencyResult:
        """Recompute all leaf hashes and root; compare against stored tree heads.

        Returns a :class:`ConsistencyResult` describing whether the log is
        internally consistent. An empty log is always consistent. The root is
        recomputed under the log's root version, and every stored head must
        carry that version (ADR-0313).
        """
        errors: list[str] = []
        version = self._root_version

        rows = self._conn.execute(
            "SELECT leaf_index, leaf_hash, entry_json FROM leaves ORDER BY leaf_index"
        ).fetchall()

        for row in rows:
            entry_bytes = row[2].encode("utf-8")
            expected = _leaf_hash(entry_bytes)
            if expected != row[1]:
                errors.append(
                    f"leaf {row[0]}: stored={row[1][:16]}… "
                    f"expected={expected[:16]}…"
                )

        leaf_hashes = [row[1] for row in rows]
        computed_root = compute_root(leaf_hashes, root_version=version)

        stored_head = self._conn.execute(
            "SELECT root_hash FROM tree_heads WHERE tree_size = ?",
            (len(rows),),
        ).fetchone()

        if stored_head is None and len(rows) > 0:
            errors.append(f"no tree_head found for size {len(rows)}")
        elif stored_head is not None and stored_head[0] != computed_root:
            errors.append(
                f"root mismatch at size {len(rows)}: "
                f"stored={stored_head[0][:16]}… "
                f"computed={computed_root[:16]}…"
            )

        mixed = self._conn.execute(
            "SELECT COUNT(*) FROM tree_heads WHERE COALESCE(root_version, 1) != ?",
            (version,),
        ).fetchone()
        if mixed is not None and int(mixed[0]) > 0:
            errors.append(
                f"{int(mixed[0])} tree head(s) carry a root_version other than this log's "
                f"(v{version}) — written by a NovaFabric release that predates ADR-0313?"
            )

        return ConsistencyResult(
            leaf_count=len(rows),
            root_hash=computed_root,
            consistent=len(errors) == 0,
            errors=errors,
            root_version=version,
        )


# ---------------------------------------------------------------------------
# PostgresMerkleLog — Postgres-backed, append-only (Scale-S4)
# ---------------------------------------------------------------------------

_PG_SCHEMA = """
CREATE SEQUENCE IF NOT EXISTS nova_seal_leaf_seq START 0 MINVALUE 0;

CREATE TABLE IF NOT EXISTS nova_seal_leaves (
    leaf_index  BIGINT PRIMARY KEY DEFAULT nextval('nova_seal_leaf_seq'),
    leaf_hash   TEXT   NOT NULL,
    entry_json  TEXT   NOT NULL
);

CREATE TABLE IF NOT EXISTS nova_seal_tree_heads (
    tree_size  BIGINT PRIMARY KEY,
    root_hash  TEXT   NOT NULL
);

-- ADR-0313, additive: NULL means a v1 head (written before the column existed).
ALTER TABLE nova_seal_tree_heads ADD COLUMN IF NOT EXISTS root_version SMALLINT;

CREATE TABLE IF NOT EXISTS nova_seal_log_meta (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);

CREATE OR REPLACE FUNCTION _nova_seal_reject_leaf_mutate()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'nova_seal_leaves is append-only; % is forbidden', TG_OP;
END;
$$;

DROP TRIGGER IF EXISTS reject_leaf_delete ON nova_seal_leaves;
CREATE TRIGGER reject_leaf_delete
    BEFORE DELETE ON nova_seal_leaves
    FOR EACH ROW EXECUTE FUNCTION _nova_seal_reject_leaf_mutate();

DROP TRIGGER IF EXISTS reject_leaf_update ON nova_seal_leaves;
CREATE TRIGGER reject_leaf_update
    BEFORE UPDATE ON nova_seal_leaves
    FOR EACH ROW EXECUTE FUNCTION _nova_seal_reject_leaf_mutate();
"""

_SAMPLE_SIZE = 1_000  # leaves spot-checked in verify_consistency() fast path


class PostgresMerkleLog:
    """Postgres-backed append-only Merkle log for NovaSeal (Scale-S4).

    Uses psycopg (psycopg3).  Connection is established lazily on first use.
    Install: ``pip install novafabric[seal-postgres]``

    ``verify_consistency()`` samples the entry_json re-hash but always recomputes
    the root from every stored leaf hash, so it is O(N) even on the fast path
    (~1.9 s at 1M entries).  That cost is the guarantee (ADR-0268); the former
    Scale-S4 target of p99 < 200 ms is **withdrawn** —
    see the module docstring.  Pass ``full=True`` to additionally re-hash every
    entry_json.

    Root version (ADR-0313): as for :class:`MerkleLog` — a new, empty log writes
    v2; a log created before ADR-0313 stays v1; *root_version* only requires one.
    """

    def __init__(self, dsn: str, *, root_version: int | None = None) -> None:
        self._dsn = dsn
        self._conn: Any = None  # psycopg.Connection, opened lazily
        self._requested_root_version = (
            None if root_version is None else _check_root_version(root_version)
        )
        self._resolved_root_version: int | None = None

    # ------------------------------------------------------------------
    # Internal connection management
    # ------------------------------------------------------------------

    def _ensure_connected(self) -> None:
        if self._conn is not None:
            return
        try:
            import psycopg
        except ImportError as exc:
            raise ImportError(
                "psycopg is required for PostgresMerkleLog. "
                "Install: pip install novafabric[seal-postgres]"
            ) from exc
        self._conn = psycopg.connect(self._dsn, autocommit=False)
        with self._conn.cursor() as cur:
            cur.execute(_PG_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001
                pass
            self._conn = None

    @property
    def root_version(self) -> int:
        """The root version every tree head of this log is computed under (ADR-0313)."""
        if self._resolved_root_version is not None:
            return self._resolved_root_version
        self._ensure_connected()
        select = "SELECT value FROM nova_seal_log_meta WHERE key = %s"
        with self._conn.cursor() as cur:
            cur.execute(select, (_ROOT_VERSION_KEY,))
            row = cur.fetchone()
            if row is None:
                cur.execute("SELECT COUNT(*) FROM nova_seal_leaves")
                count_row = cur.fetchone()
                has_leaves = bool(count_row and int(count_row[0]) > 0)
                version = _resolve_log_root_version(
                    None, has_leaves, self._requested_root_version, "Postgres Merkle log"
                )
                cur.execute(
                    "INSERT INTO nova_seal_log_meta (key, value) VALUES (%s, %s)"
                    " ON CONFLICT (key) DO NOTHING",
                    (_ROOT_VERSION_KEY, str(version)),
                )
                cur.execute(select, (_ROOT_VERSION_KEY,))
                row = cur.fetchone() or (str(version),)
        self._conn.commit()  # never leave an implicit transaction open
        self._resolved_root_version = _resolve_log_root_version(
            str(row[0]), True, self._requested_root_version, "Postgres Merkle log"
        )
        return self._resolved_root_version

    def _all_leaf_hashes(self) -> list[str]:
        self._ensure_connected()
        with self._conn.cursor() as cur:
            cur.execute("SELECT leaf_hash FROM nova_seal_leaves ORDER BY leaf_index")
            return [row[0] for row in cur.fetchall()]

    # ------------------------------------------------------------------
    # Append
    # ------------------------------------------------------------------

    def append(self, entry: dict[str, object]) -> dict[str, object]:
        """Append an entry and return the log entry dict (same shape as MerkleLog)."""
        self._ensure_connected()
        version = self.root_version
        entry_bytes = json.dumps(entry, sort_keys=True, separators=(",", ":")).encode("utf-8")
        leaf_hash = _leaf_hash(entry_bytes)
        entry_json = entry_bytes.decode("utf-8")

        with self._conn.transaction():
            with self._conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO nova_seal_leaves (leaf_hash, entry_json)"
                    " VALUES (%s, %s) RETURNING leaf_index",
                    (leaf_hash, entry_json),
                )
                leaf_index: int = cur.fetchone()[0]

                # Recompute root using all stored leaf hashes
                cur.execute(
                    "SELECT leaf_hash FROM nova_seal_leaves ORDER BY leaf_index"
                )
                all_hashes = [row[0] for row in cur.fetchall()]
                root_hash = compute_root(all_hashes, root_version=version)
                tree_size = len(all_hashes)

                cur.execute(
                    "INSERT INTO nova_seal_tree_heads (tree_size, root_hash, root_version)"
                    " VALUES (%s, %s, %s)"
                    " ON CONFLICT (tree_size) DO UPDATE SET root_hash = EXCLUDED.root_hash,"
                    " root_version = EXCLUDED.root_version",
                    (tree_size, root_hash, version),
                )

        result: dict[str, object] = {
            "leaf_index": leaf_index,
            "leaf_hash": leaf_hash,
            "root_hash": root_hash,
            "tree_size": tree_size,
            "root_version": version,  # additive (ADR-0313)
            "entry": dict(entry),
        }
        # A BIGSERIAL can leave gaps (rolled-back inserts), in which case the leaf's
        # position differs from leaf_index and a proof would not verify against it —
        # carry the proof only when the two agree.
        if all_hashes and all_hashes[-1] == leaf_hash and leaf_index == tree_size - 1:
            result["inclusion_proof"] = _compute_inclusion_proof(
                leaf_index, all_hashes, version
            )
        return result

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def current_root(self) -> str | None:
        self._ensure_connected()
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT root_hash FROM nova_seal_tree_heads ORDER BY tree_size DESC LIMIT 1"
            )
            row = cur.fetchone()
        return row[0] if row else None

    def tree_size(self) -> int:
        self._ensure_connected()
        with self._conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM nova_seal_leaves")
            row = cur.fetchone()
        return int(row[0])

    def get_leaf(self, leaf_index: int) -> dict[str, object] | None:
        self._ensure_connected()
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT entry_json FROM nova_seal_leaves WHERE leaf_index = %s",
                (leaf_index,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        result: dict[str, object] = json.loads(row[0])
        return result

    def get_leaf_hash(self, leaf_index: int) -> str:
        """Return the stored leaf_hash for leaf_index (used for inclusion proof verification)."""
        self._ensure_connected()
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT leaf_hash FROM nova_seal_leaves WHERE leaf_index = %s",
                (leaf_index,),
            )
            row = cur.fetchone()
        if row is None:
            raise MerkleError(f"leaf_index {leaf_index} not found")
        return str(row[0])

    def find_leaf_index(self, leaf_hash: str) -> int | None:
        """Return the lowest leaf_index whose leaf_hash is *leaf_hash* (see MerkleLog)."""
        self._ensure_connected()
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT MIN(leaf_index) FROM nova_seal_leaves WHERE leaf_hash = %s",
                (leaf_hash,),
            )
            row = cur.fetchone()
        return None if row is None or row[0] is None else int(row[0])

    def has_entries_for_keyid(self, keyid: str) -> bool:
        """True when the log holds a seal entry whose ``keyid`` is *keyid* (see MerkleLog)."""
        if not _is_keyid(keyid):
            return False
        self._ensure_connected()
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM nova_seal_leaves WHERE entry_json LIKE %s LIMIT 1",
                (_keyid_pattern(keyid),),
            )
            row = cur.fetchone()
        return row is not None

    def get_inclusion_proof(self, leaf_index: int) -> list[str]:
        version = self.root_version
        return _compute_inclusion_proof(leaf_index, self._all_leaf_hashes(), version)

    def verify_entry(self, leaf_index: int) -> bool:
        version = self.root_version
        all_hashes = self._all_leaf_hashes()
        if leaf_index < 0 or leaf_index >= len(all_hashes):
            return False
        root = self.current_root()
        if root is None:
            return False
        proof = _compute_inclusion_proof(leaf_index, all_hashes, version)
        return verify_inclusion_proof(
            all_hashes[leaf_index], leaf_index, proof, root, len(all_hashes),
            root_version=version,
        )

    def compute_root_at_size(
        self, tree_size: int, *, root_version: int | None = None
    ) -> str | None:
        """Recompute the root of the first *tree_size* stored leaves (see MerkleLog)."""
        version = self.root_version if root_version is None else root_version
        all_hashes = self._all_leaf_hashes()
        if not _is_size(tree_size) or tree_size > len(all_hashes):
            return None
        return compute_root(all_hashes[:tree_size], root_version=version)

    def consistency_proof(
        self, old_size: int, new_size: int | None = None
    ) -> dict[str, Any]:
        """Build a consistency proof from *old_size* to *new_size* (ADR-0041 v0.2)."""
        version = self.root_version
        all_hashes = self._all_leaf_hashes()
        return _build_consistency_proof(
            all_hashes,
            old_size,
            new_size if new_size is not None else len(all_hashes),
            version,
        )

    def root_at_size(self, tree_size: int) -> str | None:
        """Stored tree head at *tree_size*, if recorded."""
        self._ensure_connected()
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT root_hash FROM nova_seal_tree_heads WHERE tree_size = %s",
                (tree_size,),
            )
            row = cur.fetchone()
        return row[0] if row else None

    # ------------------------------------------------------------------
    # Consistency verification
    # ------------------------------------------------------------------

    def verify_consistency(self, *, full: bool = False) -> ConsistencyResult:
        """Verify Merkle log consistency.

        Fast path (default): spot-checks up to SAMPLE_SIZE random leaves, then
        reads all leaf_hashes (not entry_json) to recompute and verify the root.
        "Fast" is relative to ``full=True`` only — the root pass is O(N) and
        measures ~1.9 s at 1M entries.  That pass is the guarantee and is never
        sampled (ADR-0268); the former Scale-S4 target of p99 < 200 ms is
        withdrawn.  See the module docstring for the measurement and why.

        Full audit (``full=True``): also re-hashes every entry_json to verify
        each stored leaf_hash.  O(N) Python SHA-256 calls; slow at large N.
        """
        self._ensure_connected()
        errors: list[str] = []
        version = self.root_version

        with self._conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM nova_seal_leaves")
            leaf_count: int = int(cur.fetchone()[0])

        if leaf_count == 0:
            return ConsistencyResult(
                leaf_count=0, root_hash=_empty_hash(), consistent=True, root_version=version
            )

        # ------ Sampled spot-check (fast path always runs) ------
        sample_size = min(_SAMPLE_SIZE, leaf_count)
        if leaf_count <= sample_size:
            # Small log: check all entries
            sample_indices: list[int] = list(range(leaf_count))
        else:
            # Cryptographic RNG: the sampled leaves are the only defense
            # against an entry whose entry_json was edited while its stored
            # leaf_hash was left intact, so the choice must be unpredictable.
            sample_indices = secrets.SystemRandom().sample(
                range(leaf_count), sample_size
            )

        with self._conn.cursor() as cur:
            placeholders = ",".join(["%s"] * len(sample_indices))
            cur.execute(
                f"SELECT leaf_index, leaf_hash, entry_json FROM nova_seal_leaves"  # noqa: S608
                f" WHERE leaf_index IN ({placeholders})",
                sample_indices,
            )
            sample_rows = cur.fetchall()

        for idx, stored_hash, entry_json in sample_rows:
            entry_bytes = entry_json.encode("utf-8")
            expected = _leaf_hash(entry_bytes)
            if expected != stored_hash:
                errors.append(
                    f"leaf {idx}: stored={stored_hash[:16]}… expected={expected[:16]}…"
                )

        # ------ Full entry re-hash (opt-in) ------
        if full and leaf_count > sample_size:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT leaf_index, leaf_hash, entry_json FROM nova_seal_leaves"
                    " ORDER BY leaf_index"
                )
                # Stream in batches to avoid loading all 1M rows at once
                while True:
                    batch = cur.fetchmany(10_000)
                    if not batch:
                        break
                    for idx, stored_hash, entry_json in batch:
                        if idx in sample_indices:
                            continue  # already checked
                        entry_bytes = entry_json.encode("utf-8")
                        expected = _leaf_hash(entry_bytes)
                        if expected != stored_hash:
                            errors.append(
                                f"leaf {idx}: stored={stored_hash[:16]}… expected={expected[:16]}…"
                            )

        # ------ Root recomputation from stored leaf_hashes ------
        # Read only leaf_hash (not entry_json) — fast streaming read.
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT leaf_hash FROM nova_seal_leaves ORDER BY leaf_index"
            )
            all_leaf_hashes: list[str] = [row[0] for row in cur.fetchall()]

        computed_root = compute_root(all_leaf_hashes, root_version=version)

        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT root_hash FROM nova_seal_tree_heads WHERE tree_size = %s",
                (leaf_count,),
            )
            stored_head = cur.fetchone()

        if stored_head is None:
            errors.append(f"no tree_head found for size {leaf_count}")
        elif stored_head[0] != computed_root:
            errors.append(
                f"root mismatch at size {leaf_count}: "
                f"stored={stored_head[0][:16]}… computed={computed_root[:16]}…"
            )

        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM nova_seal_tree_heads"
                " WHERE COALESCE(root_version, 1) <> %s",
                (version,),
            )
            mixed = cur.fetchone()
        if mixed is not None and int(mixed[0]) > 0:
            errors.append(
                f"{int(mixed[0])} tree head(s) carry a root_version other than this log's "
                f"(v{version}) — written by a NovaFabric release that predates ADR-0313?"
            )

        return ConsistencyResult(
            leaf_count=leaf_count,
            root_hash=computed_root,
            consistent=len(errors) == 0,
            errors=errors,
            root_version=version,
        )


# ---------------------------------------------------------------------------
# Factory: open_merkle_log() — routes to SQLite or Postgres by URI
# ---------------------------------------------------------------------------


def open_merkle_log(uri: Union[str, Path]) -> "MerkleLog | PostgresMerkleLog":
    """Return the appropriate Merkle log backend for *uri*.

    - ``Path`` or a plain path string → ``MerkleLog`` (SQLite, default)
    - ``postgres://``, ``postgresql://`` or ``postgresql+psycopg://`` →
      ``PostgresMerkleLog`` (connects lazily)
    - any other ``scheme:/…`` string → ``MerkleLocationError`` (a ``ValueError``):
      it is never opened, or created, as a file (audit H2)

    Install Postgres support: ``pip install novafabric[seal-postgres]``
    """
    from novafabric.trust.novaseal.log_location import normalize_location  # noqa: PLC0415

    location = normalize_location(uri)
    if isinstance(location, str):
        return PostgresMerkleLog(location)
    return MerkleLog(location)


def open_existing_merkle_log(
    location: Union[str, Path, None],
) -> "MerkleLog | PostgresMerkleLog | None":
    """Open the Merkle log at *location* for verification, or None when it does not exist.

    Verification must never *create* a log: an empty one proves nothing and
    would litter an auditor's machine (ADR-0297 S2). A Postgres DSN is opened as
    given; a SQLite path only when the file already exists.
    """
    from novafabric.trust.novaseal.log_location import normalize_location  # noqa: PLC0415

    if location is None or not str(location):
        return None
    resolved = normalize_location(location)
    if isinstance(resolved, str):
        return PostgresMerkleLog(resolved)
    if not resolved.is_file():
        return None
    return MerkleLog(resolved)
