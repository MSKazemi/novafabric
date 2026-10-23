from __future__ import annotations

import hashlib
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from novafabric._safe_path import atomic_write_bytes, write_private_file


class LocalSigner:
    """Local ed25519 signer that satisfies the dsse_sign signer protocol."""

    def __init__(self, private_key_path: Path) -> None:
        with open(private_key_path, "rb") as fh:
            key = serialization.load_pem_private_key(fh.read(), password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise ValueError(
                f"{private_key_path} is not an ed25519 private key in PEM format"
            )
        self._key: Ed25519PrivateKey = key
        self._public_key: Ed25519PublicKey = key.public_key()
        self._public_pem: bytes = self._public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        self.keyid: str = "sha256:" + hashlib.sha256(self._public_pem).hexdigest()

    def sign(self, data: bytes) -> bytes:
        return self._key.sign(data)

    @property
    def public_pem(self) -> bytes:
        return self._public_pem


def _new_keypair_pem() -> tuple[bytes, bytes]:
    key = Ed25519PrivateKey.generate()
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private_pem, public_pem


def generate_keypair(out_dir: Path) -> tuple[Path, Path]:
    """Generate a fresh ed25519 keypair into out_dir.

    The private key is written mode ``0o600`` from its first byte, atomically,
    and never through a link planted at its name.

    Returns (private_pem_path, public_pem_path).
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    private_pem, public_pem = _new_keypair_pem()
    priv_path = out_dir / "ed25519.pem"
    pub_path = out_dir / "ed25519.pub.pem"
    write_private_file(priv_path, private_pem)
    atomic_write_bytes(pub_path, public_pem)
    return priv_path, pub_path


def public_key_path_for(private_key_path: Path) -> Path:
    """``signing_key.pem`` -> ``signing_key.pub.pem`` (the public half beside a key)."""
    stem = private_key_path.name.removesuffix(".pem")
    return private_key_path.with_name(f"{stem}.pub.pem")


def ensure_signing_key(private_key_path: Path) -> Path:
    """Return *private_key_path*, creating an ed25519 key there first if none exists.

    The key is created mode ``0o600`` with an exclusive, atomic, no-follow write,
    so two concurrent callers never overwrite each other's key and a link planted
    at the name is never written through. The public half is written beside it
    (:func:`public_key_path_for`). An existing key is used as it is.
    """
    if private_key_path.exists():
        return private_key_path
    private_key_path.parent.mkdir(parents=True, exist_ok=True)
    private_pem, public_pem = _new_keypair_pem()
    try:
        write_private_file(private_key_path, private_pem, exclusive=True)
    except FileExistsError:
        return private_key_path  # a concurrent caller created it first
    atomic_write_bytes(public_key_path_for(private_key_path), public_pem)
    return private_key_path


def verify_with_pem(public_pem: bytes, pae_bytes: bytes, signature: bytes) -> bool:
    """Verify a signature against PAE bytes using a PEM-encoded public key."""
    pubkey = serialization.load_pem_public_key(public_pem)
    if not isinstance(pubkey, Ed25519PublicKey):
        raise ValueError("public key is not ed25519")
    try:
        pubkey.verify(signature, pae_bytes)
        return True
    except InvalidSignature:
        return False
