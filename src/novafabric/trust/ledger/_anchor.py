"""Local finalize anchor (ADR-0094 A) — shared by ``nova ledger`` and ``nova evidence``.

One function, :func:`anchor_capsule`, performs the offline-safe local anchor
path: build a per-stream sidecar chain for every ``.jsonl`` evidence stream,
build + DSSE-sign a multi-stream checkpoint, and bind the signature digest as
a local finalize anchor (``method: none`` — no TSA required). The ``.jsonl``
evidence streams are never modified (additive invariant).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from novafabric.trust.ledger._chain import write_chain
from novafabric.trust.ledger._checkpoint import (
    CheckpointRecord,
    build_checkpoint,
    checkpoint_path,
    sign_checkpoint,
)


class _PemSigner(Protocol):
    """A DSSE signer that also exposes its public key PEM (e.g. ``LocalSigner``)."""

    keyid: str

    @property
    def public_pem(self) -> bytes: ...

    def sign(self, data: bytes) -> bytes: ...


def discover_jsonl_streams(capsule_dir: Path) -> list[str]:
    """Names of every ``.jsonl`` evidence stream in the capsule (sorted)."""
    return sorted(p.name[: -len(".jsonl")] for p in capsule_dir.glob("*.jsonl"))


def anchor_capsule(
    capsule_dir: Path,
    signer: _PemSigner,
    *,
    node_id: str = "local",
    epoch: int = 0,
    agent_id: str | None = None,
    principal: str | None = None,
) -> CheckpointRecord:
    """Run the local finalize anchor path over every ``.jsonl`` stream.

    Writes ``<capsule>/.ledger/<stream>.chain.json`` per stream and a signed
    ``<capsule>/.ledger/checkpoint.json`` with a local finalize anchor.
    Returns the persisted :class:`CheckpointRecord`.

    Raises :class:`ValueError` when the capsule has no ``.jsonl`` streams.
    """
    streams = discover_jsonl_streams(capsule_dir)
    if not streams:
        raise ValueError(f"no .jsonl evidence streams under {capsule_dir}")

    for stream in streams:
        write_chain(capsule_dir, stream)

    record = build_checkpoint(
        capsule_dir,
        node_id=node_id,
        epoch=epoch,
        epoch_pubkey=signer.public_pem.decode(),
        agent_id=agent_id,
        principal=principal,
    )
    sign_checkpoint(record, signer, capsule_dir=capsule_dir, persist=False)

    # Local finalize anchor: bind the signature digest (offline-safe; no TSA).
    sig_value = str((record.signature or {}).get("value", ""))
    record.anchor = {
        "method": "none",
        "anchored_value": "sha256:" + hashlib.sha256(sig_value.encode()).hexdigest(),
        "anchored_at": datetime.now(timezone.utc).isoformat(),
    }
    path = checkpoint_path(capsule_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record.to_record(), indent=2) + "\n")
    return record
