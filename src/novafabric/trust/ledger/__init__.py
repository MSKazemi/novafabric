"""Adversary-Anchored Accountability Ledger (ADR-0094, capability A).

A per-stream sidecar hash-chain layer plus a multi-stream, forward-securely
signed, externally anchored checkpoint, composed entirely from already-accepted
NovaSeal primitives (ADR-0089 ratchet, ADR-0041 Merkle log, ADR-0070/0071
external anchors). The ``.jsonl`` evidence streams are never modified — the
chains are sidecars at ``capsule_dir/.ledger/<stream>.chain.json`` (additive
invariant).

This package is opt-in and additive; it never replaces ``audit/_log.py`` and
never changes existing capsule behavior by default.
"""

from __future__ import annotations

from novafabric.trust.ledger._anchor import anchor_capsule, discover_jsonl_streams
from novafabric.trust.ledger._chain import (
    ChainLink,
    ChainTamperClass,
    ChainVerdict,
    build_chain,
    verify_chain,
    write_chain,
)
from novafabric.trust.ledger._checkpoint import (
    CHECKPOINT_PREDICATE_TYPE,
    CheckpointRecord,
    IdentityBinding,
    StreamHead,
    build_checkpoint,
    sign_checkpoint,
)
from novafabric.trust.ledger._verify import (
    LedgerExit,
    LedgerVerdict,
    exit_code_for,
    verify_ledger,
)

__all__ = [
    "CHECKPOINT_PREDICATE_TYPE",
    "ChainLink",
    "ChainTamperClass",
    "ChainVerdict",
    "CheckpointRecord",
    "IdentityBinding",
    "LedgerExit",
    "LedgerVerdict",
    "StreamHead",
    "anchor_capsule",
    "build_chain",
    "build_checkpoint",
    "discover_jsonl_streams",
    "exit_code_for",
    "sign_checkpoint",
    "verify_chain",
    "verify_ledger",
    "write_chain",
]
