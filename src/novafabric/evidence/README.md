# `novafabric.evidence`

**Evidence Bundle assembly** (ADR-0011): turns a capsule into a signed ZIP
bundle. Provides `EvidenceBundleBuilder`, local ed25519 signing
(`LocalSigner`, `generate_keypair`), DSSE/in-toto primitives, and RFC 6962-style
Merkle roots over capsule files.

**Not to be confused with [`novafabric.evidence_fabric`](../evidence_fabric/) —
the streaming accumulator stack.** `evidence` = build one signed bundle;
`evidence_fabric` = accumulate evidence events at scale.
