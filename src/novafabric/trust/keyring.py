from __future__ import annotations

import base64
import getpass
import hashlib
import socket
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

_KEYRING_DIR = Path.home() / ".config" / "novafabric" / "keyring"


def _default_identity() -> str:
    return f"{getpass.getuser()}@{socket.gethostname()}"


def _key_path(identity: str) -> Path:
    safe = identity.replace("/", "_").replace("@", "_at_")
    return _KEYRING_DIR / f"{safe}.pem"


def _fingerprint(public_key: Ed25519PublicKey) -> str:
    raw = public_key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return hashlib.sha256(raw).hexdigest()[:16]


def ensure_keypair(identity: str | None = None) -> tuple[Ed25519PrivateKey, str]:
    """Return (private_key, fingerprint) for *identity*, generating if absent."""
    ident = identity or _default_identity()
    path = _key_path(ident)
    if path.exists():
        pem = path.read_bytes()
        private_key = serialization.load_pem_private_key(pem, password=None)
        if not isinstance(private_key, Ed25519PrivateKey):
            raise ValueError(f"Key at {path} is not Ed25519")
    else:
        private_key = Ed25519PrivateKey.generate()
        path.parent.mkdir(parents=True, exist_ok=True)
        pem = private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        path.write_bytes(pem)
        path.chmod(0o600)
    fp = _fingerprint(private_key.public_key())
    return private_key, fp


def sign_payload(private_key: Ed25519PrivateKey, payload: bytes) -> str:
    """Return base64url-encoded Ed25519 signature over *payload*."""
    sig = private_key.sign(payload)
    return base64.urlsafe_b64encode(sig).rstrip(b"=").decode()


def verify_sig(public_key: Ed25519PublicKey, sig_b64: str, payload: bytes) -> bool:
    """Return True if *sig_b64* is a valid Ed25519 signature over *payload*."""
    padding = "=" * (-len(sig_b64) % 4)
    sig_bytes = base64.urlsafe_b64decode(sig_b64 + padding)
    try:
        public_key.verify(sig_bytes, payload)
        return True
    except Exception:
        return False


def canonical_payload(proposal_id: str, name: str, version: str, to_status: str) -> bytes:
    return f"{proposal_id}|{name}@{version}:{to_status}".encode()
