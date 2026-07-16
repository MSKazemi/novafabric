"""Evidence Provenance Merkle proof-tree — JSON projection of a sealed capsule's tree.

ADR-0172 (data slice). Pure and read-only: project a capsule's Merkle tree into a node model —
``leaf → intermediate → seal-root → tsr`` — for the interactive proof-tree view. The tree is
derived from :func:`novafabric.trust.novaseal.merkle.merkle_layers`, the canonical layer
enumerator, so the projected structure matches the sealed root byte-for-byte; this module never
reimplements the leaf/node hashing or the padding rule (ADR-0172 Consequences c).

This is the Python/JSON half of feature F-04: it feeds the `web/` interactive proof tree that
ADR-0172 describes; it is not that view. No capsule-schema change.

Privacy (ADR-0009): leaf nodes are labelled with the **field path only** — never the value. A
:class:`ProofNode` has no value field; only a short hash prefix, node type, verify state, and an
optional path label.
"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel

from novafabric.trust.novaseal.merkle import merkle_layers


class NodeType(str, Enum):
    leaf = "leaf"
    intermediate = "intermediate"
    seal_root = "seal-root"
    tsr = "tsr"


class VerifyState(str, Enum):
    verified = "verified"
    mismatch = "mismatch"
    unverified = "unverified"


class ProofNode(BaseModel):
    id: str
    node_type: NodeType
    hash_prefix: str  # first/last 6 hex; full hash is never carried here
    verify_state: VerifyState
    label: str | None = None  # leaf field path (ADR-0009) — never a value
    layer: int
    # Intentionally NO value field — the ADR-0009 redaction invariant, enforced at the type level.


class ProofTree(BaseModel):
    capsule_id: str | None = None
    sealed: bool
    computed_root: str | None
    nodes: list[ProofNode]


def _prefix(h: str) -> str:
    return h if len(h) <= 14 else f"{h[:6]}..{h[-6:]}"


def build_proof_tree(
    leaf_hashes: list[str],
    *,
    leaf_labels: list[str] | None = None,
    sealed_root: str | None = None,
    tsr_hash: str | None = None,
    capsule_id: str | None = None,
) -> ProofTree:
    """Project a capsule's leaf hashes (+ optional seal root / TSR) into a :class:`ProofTree`.

    ``leaf_labels`` are field paths, positionally aligned to ``leaf_hashes`` (ADR-0009: paths,
    never values). ``sealed_root`` (if given) is compared against the recomputed root: a match
    verifies the seal-root node, a mismatch flags it. Without a ``sealed_root`` the tree is
    ``unsealed`` and every node is ``unverified`` (a per-leaf client-side recompute is the `web/`
    view's job, not this projection's).
    """
    if not leaf_hashes:
        return ProofTree(
            capsule_id=capsule_id, sealed=sealed_root is not None, computed_root=None, nodes=[]
        )

    layers = merkle_layers(leaf_hashes)
    computed_root = layers[-1][0]
    sealed = sealed_root is not None
    root_ok = computed_root == sealed_root if sealed else False
    top = len(layers) - 1

    nodes: list[ProofNode] = []
    for li, layer in enumerate(layers):
        for xi, h in enumerate(layer):
            if li == top:
                node_type = NodeType.seal_root
            elif li == 0:
                node_type = NodeType.leaf
            else:
                node_type = NodeType.intermediate

            if not sealed:
                verify_state = VerifyState.unverified
            elif node_type is NodeType.seal_root:
                verify_state = VerifyState.verified if root_ok else VerifyState.mismatch
            else:
                verify_state = VerifyState.verified if root_ok else VerifyState.unverified

            label = None
            if node_type is NodeType.leaf and leaf_labels is not None and xi < len(leaf_labels):
                label = leaf_labels[xi]

            nodes.append(
                ProofNode(
                    id=f"L{li}N{xi}",
                    node_type=node_type,
                    hash_prefix=_prefix(h),
                    verify_state=verify_state,
                    label=label,
                    layer=li,
                )
            )

    if tsr_hash is not None:
        # TSR verification (RFC 3161) is out of scope for this projection → unverified in v0.
        nodes.append(
            ProofNode(
                id="TSR",
                node_type=NodeType.tsr,
                hash_prefix=_prefix(tsr_hash),
                verify_state=VerifyState.unverified,
                label="RFC 3161 timestamp",
                layer=top + 1,
            )
        )

    return ProofTree(
        capsule_id=capsule_id, sealed=sealed, computed_root=computed_root, nodes=nodes
    )
