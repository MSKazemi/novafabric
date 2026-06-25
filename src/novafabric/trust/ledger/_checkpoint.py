"""Multi-stream ledger checkpoint (ADR-0094 A, ``trust/ledger/_checkpoint.py``).

A :class:`CheckpointRecord` aggregates the sealed head of every per-stream
sidecar chain in a capsule (``capsule_dir/.ledger/<stream>.chain.json``) plus
the identity binding (node epoch + agent + principal), matching
``schemas/ledger-checkpoint-v1.schema.json``.

Signing order (so the signature covers exactly the body and post-signing fields
are not self-referential):

1. assemble the body excluding ``signature``, ``merkle``, ``anchor``;
2. DSSE-sign the canonical body with the supplied signer (the ADR-0089
   ``RatchetSigner`` in production; any ``sign(bytes)->bytes`` + ``keyid``
   signer in tests) → ``signature``;
3. optionally append the signed checkpoint as a leaf to the ADR-0041
   ``MerkleLog`` → ``merkle``;
4. (anchoring is performed by the CLI/caller → ``anchor``).

The full record is persisted to ``capsule_dir/.ledger/checkpoint.json``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field, PrivateAttr

from novafabric.capsule.ulid_util import new_ulid
from novafabric.evidence.intoto import dsse_sign, make_intoto_statement
from novafabric.trust.ledger._chain import (
    LEDGER_DIRNAME,
    chain_path_for,
    jsonl_path_for,
    verify_chain,
)

CHECKPOINT_PREDICATE_TYPE = "https://novafabric.io/ledger-checkpoint/v1"
CHECKPOINT_PAYLOAD_TYPE = "application/vnd.novafabric.ledger-checkpoint+json"
CHECKPOINT_FILENAME = "checkpoint.json"

CheckpointKind = Literal["finalize", "batch", "interim"]


class _Signer(Protocol):
    keyid: str

    def sign(self, data: bytes) -> bytes: ...


class _MerkleLog(Protocol):
    def append(self, entry: dict[str, object]) -> dict[str, object]: ...

    def get_inclusion_proof(self, leaf_index: int) -> list[str]: ...


class StreamHead(BaseModel):
    """Sealed head of one per-stream sidecar chain."""

    stream: str
    seq_high: int
    record_count: int
    head_link_hash: str
    chain_file: str | None = None
    jsonl_file: str | None = None


class IdentityBinding(BaseModel):
    """Binds node (epoch key) + agent + principal under one signature."""

    node_id: str
    epoch: int
    epoch_pubkey: str
    agent_id: str | None = None
    principal: str | None = None


class CheckpointRecord(BaseModel):
    """Multi-stream checkpoint record (``ledger-checkpoint-v1``)."""

    schema_version: str = "1.0.0"
    checkpoint_id: str = Field(default_factory=new_ulid)
    capsule_id: str
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    kind: CheckpointKind = "finalize"
    prev_checkpoint_id: str | None = None
    streams: list[StreamHead]
    identity: IdentityBinding
    merkle: dict[str, Any] | None = None
    anchor: dict[str, Any] | None = None
    signature: dict[str, Any] | None = None
    extensions: dict[str, Any] | None = None

    _capsule_dir: Path | None = PrivateAttr(default=None)

    def signing_body(self) -> dict[str, Any]:
        """The canonical body to sign — excludes signature/merkle/anchor."""
        return self.model_dump(
            exclude={"signature", "merkle", "anchor"}, exclude_none=True
        )

    def to_record(self) -> dict[str, Any]:
        """Full record for persistence / schema validation."""
        return self.model_dump(exclude_none=True)


def _head_link_hash(capsule_dir: Path, stream_name: str) -> tuple[int, int, str]:
    verdict = verify_chain(capsule_dir, stream_name)
    head = "sha256:" + verdict.head_link_hash if verdict.head_link_hash else (
        "sha256:" + "0" * 64
    )
    return verdict.seq_high, verdict.record_count, head


def _discover_streams(capsule_dir: Path) -> list[str]:
    ledger_dir = capsule_dir / LEDGER_DIRNAME
    if not ledger_dir.exists():
        return []
    streams: list[str] = []
    for path in sorted(ledger_dir.glob("*.chain.json")):
        streams.append(path.name[: -len(".chain.json")])
    return streams


def build_checkpoint(
    capsule_dir: Path,
    *,
    node_id: str,
    epoch: int,
    epoch_pubkey: str,
    agent_id: str | None = None,
    principal: str | None = None,
    kind: CheckpointKind = "finalize",
    prev_checkpoint_id: str | None = None,
) -> CheckpointRecord:
    """Assemble a :class:`CheckpointRecord` over every sealed sidecar chain.

    Raises :class:`ValueError` if no sidecar chains have been written.
    """
    stream_names = _discover_streams(capsule_dir)
    if not stream_names:
        raise ValueError(
            f"no sidecar chains under {capsule_dir / LEDGER_DIRNAME}; "
            "run write_chain() first"
        )

    heads: list[StreamHead] = []
    for stream_name in stream_names:
        seq_high, record_count, head = _head_link_hash(capsule_dir, stream_name)
        jsonl = jsonl_path_for(capsule_dir, stream_name)
        chain = chain_path_for(capsule_dir, stream_name)
        heads.append(
            StreamHead(
                stream=stream_name,
                seq_high=seq_high,
                record_count=record_count,
                head_link_hash=head,
                chain_file=str(chain.relative_to(capsule_dir)),
                jsonl_file=(
                    str(jsonl.relative_to(capsule_dir)) if jsonl.exists() else None
                ),
            )
        )

    record = CheckpointRecord(
        capsule_id=capsule_dir.name,
        kind=kind,
        prev_checkpoint_id=prev_checkpoint_id,
        streams=heads,
        identity=IdentityBinding(
            node_id=node_id,
            epoch=epoch,
            epoch_pubkey=epoch_pubkey,
            agent_id=agent_id,
            principal=principal,
        ),
    )
    record._capsule_dir = capsule_dir
    return record


def checkpoint_path(capsule_dir: Path) -> Path:
    """Location of the persisted checkpoint record."""
    return capsule_dir / LEDGER_DIRNAME / CHECKPOINT_FILENAME


def sign_checkpoint(
    record: CheckpointRecord,
    signer: _Signer,
    *,
    capsule_dir: Path | None = None,
    merkle_log: _MerkleLog | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    """DSSE-sign the checkpoint body, optionally anchor in the Merkle log.

    Returns the DSSE envelope. When *persist* is true (default) the full
    checkpoint record — including the freshly populated ``signature`` and
    ``merkle`` blocks — is written to ``<capsule>/.ledger/checkpoint.json``.

    The signed body deliberately excludes ``signature``, ``merkle`` and
    ``anchor`` so the post-signing fields are not self-referential.
    """
    statement = make_intoto_statement(
        predicate_type=CHECKPOINT_PREDICATE_TYPE,
        subject_name=f"run-capsule/{record.capsule_id}",
        subject_sha256="sha256:" + "0" * 64,
        predicate=record.signing_body(),
    )
    envelope = dsse_sign(statement, signer)

    record.signature = {
        "algorithm": "ed25519",
        "value": envelope["signatures"][0]["sig"],
        "payload_type": CHECKPOINT_PAYLOAD_TYPE,
    }

    if merkle_log is not None:
        leaf = merkle_log.append(record.signing_body())
        leaf_index_obj = leaf["leaf_index"]
        assert isinstance(leaf_index_obj, int)
        leaf_index = leaf_index_obj
        record.merkle = {
            "root": "sha256:" + str(leaf["root_hash"]),
            "leaf_hash": "sha256:" + str(leaf["leaf_hash"]),
            "log_index": leaf_index,
            "inclusion_proof": [
                "sha256:" + h for h in merkle_log.get_inclusion_proof(leaf_index)
            ],
        }

    target_dir = capsule_dir if capsule_dir is not None else record._capsule_dir
    if persist and target_dir is not None:
        path = checkpoint_path(target_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        import json

        path.write_text(json.dumps(record.to_record(), indent=2) + "\n")

    return envelope
