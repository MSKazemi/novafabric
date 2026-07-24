"""Guard: the two Merkle constructions are non-interoperable (ADR-0218).

`evidence/merkle.py` is RFC 6962 (split at the largest power of two);
`trust/novaseal/merkle.py` pads odd levels by duplicating the last node. BOTH
domain-separate (0x00 leaf / 0x01 node), so they *agree* for power-of-two leaf
counts (1, 2, 4, …) — which is exactly what makes the divergence at 3, 5, 6, 7
a silent hazard. These tests pin the boundary so any accidental convergence or
cross-construction mixing is caught.
"""

from __future__ import annotations

from novafabric.evidence.merkle import _inner as ev_inner
from novafabric.evidence.merkle import _leaf as ev_leaf
from novafabric.evidence.merkle import _merkle_root as ev_root
from novafabric.trust.novaseal.merkle import _compute_root as ns_root
from novafabric.trust.novaseal.merkle import _leaf_hash as ns_leaf


def _payloads(n: int) -> list[bytes]:
    return [f"entry-{i}".encode() for i in range(n)]


def test_leaf_hashing_agrees() -> None:
    """Both use sha256(0x00 || data) for leaves — identical leaf bytes."""
    data = b"entry-0"
    assert ev_leaf(data).hex() == ns_leaf(data)


def test_roots_agree_on_power_of_two_counts() -> None:
    """The trap: for 1, 2, 4 leaves the two constructions coincide."""
    for n in (1, 2, 4):
        payloads = _payloads(n)
        ev = ev_root([ev_leaf(p) for p in payloads]).hex()
        ns = ns_root([ns_leaf(p) for p in payloads])
        assert ev == ns, f"expected agreement at n={n}"


def test_roots_diverge_on_non_power_of_two_counts() -> None:
    """For 3, 5, 6, 7 leaves the padding vs split rule yields different roots.

    This is the concrete non-interoperability: a proof produced by one
    construction will not verify against the other's root.
    """
    for n in (3, 5, 6, 7):
        payloads = _payloads(n)
        ev = ev_root([ev_leaf(p) for p in payloads]).hex()
        ns = ns_root([ns_leaf(p) for p in payloads])
        assert ev != ns, f"expected divergence at n={n}"


def test_novaseal_pads_odd_level_by_duplication() -> None:
    """Document the specific NovaSeal padding rule vs the RFC 6962 split."""
    p = _payloads(3)
    leaves_hex = [ns_leaf(x) for x in p]
    # NovaSeal n=3: node(node(l0,l1), node(l2,l2)).
    from novafabric.trust.novaseal.merkle import _node_hash as ns_node

    expected = ns_node(ns_node(leaves_hex[0], leaves_hex[1]), ns_node(leaves_hex[2], leaves_hex[2]))
    assert ns_root(leaves_hex) == expected

    # RFC 6962 n=3: inner(inner(l0,l1), l2) — no duplication of l2.
    ev_leaves = [ev_leaf(x) for x in p]
    ev_expected = ev_inner(ev_inner(ev_leaves[0], ev_leaves[1]), ev_leaves[2])
    assert ev_root(ev_leaves) == ev_expected
