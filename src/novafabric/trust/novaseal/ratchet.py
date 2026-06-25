"""Forward-secure per-node signing key ratchet (ADR-0089, gap-015).

Epoch chain: a per-node seed ``K_0``; ``K_{i+1} = HKDF-SHA256(K_i,
info="novaseal-ratchet-v1")``; the epoch-i Ed25519 signing key is derived
deterministically from ``K_i``.  After rotation the previous chain key is
best-effort securely erased (overwrite + delete — honest limits: journaling
filesystems and SSD wear-levelling may retain stale blocks; see ADR-0089).

Security property: compromise of a node at epoch N cannot forge signatures
for epochs < N.  Current/future epochs remain forgeable until detected —
this module never claims otherwise.

Everything here is opt-in (experimental); the legacy static-key NovaSeal
paths remain the default and continue to verify.
"""

from __future__ import annotations

import os
import secrets
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from pydantic import BaseModel

from novafabric._paths import nova_home

_CHAIN_INFO = b"novaseal-ratchet-v1"
_SIGNING_INFO = b"novaseal-ratchet-v1-signing"


class RatchetError(Exception):
    """Raised on ratchet state corruption or invalid operations."""


class EpochMonotonicityError(RatchetError):
    """Raised when a seal's epoch goes backwards for a node."""


class RatchetState(BaseModel):
    """Persisted per-node ratchet state (current epoch only)."""

    node_id: str
    epoch: int
    chain_key_hex: str
    rotated_at: str


class EpochRecord(BaseModel):
    """One append-only epoch-pubkey registry row."""

    node_id: str
    epoch: int
    public_key_pem: str
    rotated_at: str


def ratchet_dir() -> Path:
    """Ratchet state directory: ``$NOVAFABRIC_HOME/seal/ratchet``.

    Override with ``NOVAFABRIC_RATCHET_DIR``.
    """
    env = os.environ.get("NOVAFABRIC_RATCHET_DIR")
    return Path(env) if env else nova_home() / "seal" / "ratchet"


def _state_path(state_dir: Path, node_id: str) -> Path:
    return state_dir / f"{node_id}.json"


def _registry_path(state_dir: Path) -> Path:
    return state_dir / "epoch-registry.jsonl"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _next_chain_key(chain_key: bytes) -> bytes:
    return HKDF(
        algorithm=SHA256(), length=32, salt=None, info=_CHAIN_INFO
    ).derive(chain_key)


def _signing_key_for(chain_key: bytes) -> Ed25519PrivateKey:
    seed = HKDF(
        algorithm=SHA256(), length=32, salt=None, info=_SIGNING_INFO
    ).derive(chain_key)
    return Ed25519PrivateKey.from_private_bytes(seed)


def _public_pem(key: Ed25519PrivateKey) -> str:
    return key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()


def _secure_overwrite_delete(path: Path) -> None:
    """Best-effort secure erase: overwrite with random bytes, then unlink."""
    try:
        size = path.stat().st_size
        with open(path, "r+b") as fh:
            fh.write(secrets.token_bytes(max(size, 64)))
            fh.flush()
            os.fsync(fh.fileno())
    except OSError:
        pass
    path.unlink(missing_ok=True)


def _write_state(state_dir: Path, state: RatchetState) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    path = _state_path(state_dir, state.node_id)
    path.write_text(state.model_dump_json(indent=2) + "\n")
    path.chmod(0o600)


def _append_registry(state_dir: Path, record: EpochRecord) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    with open(_registry_path(state_dir), "a") as fh:
        fh.write(record.model_dump_json() + "\n")


def load_state(node_id: str, state_dir: Path | None = None) -> RatchetState:
    """Load the node's current ratchet state or raise :class:`RatchetError`."""
    directory = state_dir if state_dir is not None else ratchet_dir()
    path = _state_path(directory, node_id)
    if not path.exists():
        raise RatchetError(
            f"no ratchet state for node {node_id!r}; run `nova seal ratchet init`"
        )
    try:
        return RatchetState.model_validate_json(path.read_text())
    except (OSError, ValueError) as exc:
        raise RatchetError(f"corrupt ratchet state at {path}") from exc


def init_ratchet(
    node_id: str, state_dir: Path | None = None, seed: bytes | None = None
) -> RatchetState:
    """Provision epoch-0 state for *node_id*; idempotence is refused."""
    directory = state_dir if state_dir is not None else ratchet_dir()
    if _state_path(directory, node_id).exists():
        raise RatchetError(f"ratchet state already exists for node {node_id!r}")
    chain_key = seed if seed is not None else secrets.token_bytes(32)
    state = RatchetState(
        node_id=node_id,
        epoch=0,
        chain_key_hex=chain_key.hex(),
        rotated_at=_now(),
    )
    _write_state(directory, state)
    _append_registry(
        directory,
        EpochRecord(
            node_id=node_id,
            epoch=0,
            public_key_pem=_public_pem(_signing_key_for(chain_key)),
            rotated_at=state.rotated_at,
        ),
    )
    return state


def rotate(node_id: str, state_dir: Path | None = None) -> RatchetState:
    """Advance to the next epoch; best-effort erase the previous chain key."""
    directory = state_dir if state_dir is not None else ratchet_dir()
    state = load_state(node_id, directory)
    old_path = _state_path(directory, node_id)
    next_key = _next_chain_key(bytes.fromhex(state.chain_key_hex))
    new_state = RatchetState(
        node_id=node_id,
        epoch=state.epoch + 1,
        chain_key_hex=next_key.hex(),
        rotated_at=_now(),
    )
    _secure_overwrite_delete(old_path)
    _write_state(directory, new_state)
    _append_registry(
        directory,
        EpochRecord(
            node_id=node_id,
            epoch=new_state.epoch,
            public_key_pem=_public_pem(_signing_key_for(next_key)),
            rotated_at=new_state.rotated_at,
        ),
    )
    return new_state


class RatchetSigner:
    """Epoch-bound signer satisfying the ``dsse_sign`` signer protocol.

    ``keyid`` carries node and epoch (``ratchet:<node>:<epoch>``) so seal
    metadata records which epoch key signed.
    """

    def __init__(self, node_id: str, state_dir: Path | None = None) -> None:
        state = load_state(node_id, state_dir)
        self._key = _signing_key_for(bytes.fromhex(state.chain_key_hex))
        self.node_id = node_id
        self.epoch = state.epoch
        self.keyid = f"ratchet:{node_id}:{state.epoch}"

    def sign(self, data: bytes) -> bytes:
        return self._key.sign(data)

    @property
    def public_pem(self) -> bytes:
        return _public_pem(self._key).encode()


class EpochRegistry:
    """Read surface over the append-only epoch-pubkey registry."""

    def __init__(self, state_dir: Path | None = None) -> None:
        self._path = _registry_path(
            state_dir if state_dir is not None else ratchet_dir()
        )

    def records(self, node_id: str | None = None) -> list[EpochRecord]:
        if not self._path.exists():
            return []
        records: list[EpochRecord] = []
        for line in self._path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            record = EpochRecord.model_validate_json(line)
            if node_id is None or record.node_id == node_id:
                records.append(record)
        return records

    def public_key(self, node_id: str, epoch: int) -> Ed25519PublicKey:
        for record in self.records(node_id):
            if record.epoch == epoch:
                key = serialization.load_pem_public_key(
                    record.public_key_pem.encode()
                )
                if not isinstance(key, Ed25519PublicKey):
                    raise RatchetError("registry entry is not an Ed25519 key")
                return key
        raise RatchetError(f"no registry entry for {node_id!r} epoch {epoch}")

    def latest_epoch(self, node_id: str) -> int | None:
        epochs = [r.epoch for r in self.records(node_id)]
        return max(epochs) if epochs else None

    def check_monotonic(self, node_id: str, seen_epoch: int) -> None:
        """Reject a seal claiming an epoch older than the registry's latest.

        Raises :class:`EpochMonotonicityError` — old-epoch keys are erased
        after rotation, so a fresh signature from one indicates compromise
        or rollback.
        """
        latest = self.latest_epoch(node_id)
        if latest is not None and seen_epoch < latest:
            raise EpochMonotonicityError(
                f"seal epoch {seen_epoch} for node {node_id!r} is older than "
                f"registry latest {latest} (possible rollback)"
            )

    def verify(
        self, node_id: str, epoch: int, data: bytes, signature: bytes
    ) -> bool:
        """Verify a signature against the registered epoch public key."""
        from cryptography.exceptions import InvalidSignature

        try:
            self.public_key(node_id, epoch).verify(signature, data)
            return True
        except (InvalidSignature, RatchetError):
            return False
