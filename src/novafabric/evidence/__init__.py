"""Evidence Bundle assembly per ADR-0011.

Public surface:
- `EvidenceBundleBuilder` — orchestrates capsule -> ZIP bundle.
- `LocalSigner`, `generate_keypair` — local ed25519 signing.
- `dsse_sign`, `dsse_verify`, `dsse_pae` — in-toto + DSSE primitives.
- `capsule_merkle_root` — RFC 6962-style Merkle hash over capsule files.
"""
from novafabric.evidence.bundle import EvidenceBundleBuilder
from novafabric.evidence.intoto import (
    dsse_pae,
    dsse_sign,
    dsse_verify,
    make_intoto_statement,
)
from novafabric.evidence.merkle import capsule_merkle_root
from novafabric.evidence.signing import LocalSigner, generate_keypair

__all__ = [
    "EvidenceBundleBuilder",
    "LocalSigner",
    "capsule_merkle_root",
    "dsse_pae",
    "dsse_sign",
    "dsse_verify",
    "generate_keypair",
    "make_intoto_statement",
]
