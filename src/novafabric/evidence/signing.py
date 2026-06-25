from __future__ import annotations

import hashlib
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)


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


def generate_keypair(out_dir: Path) -> tuple[Path, Path]:
    """Generate a fresh ed25519 keypair into out_dir.

    Returns (private_pem_path, public_pem_path).
    """
    out_dir.mkdir(parents=True, exist_ok=True)
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
    priv_path = out_dir / "ed25519.pem"
    pub_path = out_dir / "ed25519.pub.pem"
    priv_path.write_bytes(private_pem)
    pub_path.write_bytes(public_pem)
    priv_path.chmod(0o600)
    return priv_path, pub_path


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
