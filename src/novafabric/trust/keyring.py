from __future__ import annotations

import base64
import getpass
import hashlib
import os
import socket
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from novafabric._safe_path import write_private_file

#: Environment override for the keyring directory (operators, and the hermetic test env).
KEYRING_DIR_ENV = "NOVAFABRIC_KEYRING_DIR"

#: In-process override (tests); ``None`` = resolve with :func:`keyring_dir`.
_KEYRING_DIR: Path | None = None


def keyring_dir() -> Path:
    """The promote keyring directory, resolved when called — never at import.

    User-global by design: ``~/.config/novafabric/keyring``, deliberately NOT under
    ``NOVAFABRIC_HOME``, so it survives a throwaway or per-project home.
    ``NOVAFABRIC_KEYRING_DIR`` relocates it. It used to be computed from
    ``Path.home()`` at import, so a test (or tool) that redirected ``HOME``
    afterwards still wrote keys into the developer's real keyring (review: eval
    reviewer's repro wrote two keys there).
    """
    if _KEYRING_DIR is not None:
        return _KEYRING_DIR
    configured = os.environ.get(KEYRING_DIR_ENV, "").strip()
    if configured:
        return Path(configured)
    return Path.home() / ".config" / "novafabric" / "keyring"


def _default_identity() -> str:
    return f"{getpass.getuser()}@{socket.gethostname()}"


def _key_path(identity: str) -> Path:
    safe = identity.replace("/", "_").replace("@", "_at_")
    return keyring_dir() / f"{safe}.pem"


def _fingerprint(public_key: Ed25519PublicKey) -> str:
    raw = public_key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return hashlib.sha256(raw).hexdigest()[:16]


class KeyNotFoundError(FileNotFoundError):
    """No keyring key exists for an identity and the caller did not ask to create one."""


def key_path_for(identity: str) -> Path:
    """Path of the keyring PEM for *identity* (whether or not it exists)."""
    return _key_path(identity)


def fingerprint_of(public_key: Ed25519PublicKey) -> str:
    """Keyring fingerprint: the first 16 hex chars of SHA-256 over the raw public key."""
    return _fingerprint(public_key)


def public_key_b64(public_key: Ed25519PublicKey) -> str:
    """Standard base64 of the raw 32-byte Ed25519 public key (signer trust-list form)."""
    raw = public_key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode()


def ensure_keypair(
    identity: str | None = None, *, create: bool = True
) -> tuple[Ed25519PrivateKey, str]:
    """Return (private_key, fingerprint) for *identity*.

    With ``create=True`` (the default, kept for the existing signing callers) a missing
    key is generated. With ``create=False`` a missing key raises :class:`KeyNotFoundError`
    and nothing is written: the maker-checker promote path uses this, so naming a new
    identity can never mint a key on the fly (ADR-0312). Keys for that path are created
    only by ``nova promote keygen``.
    """
    ident = identity or _default_identity()
    path = _key_path(ident)
    if path.exists():
        private_key = _load_key(path)
    elif not create:
        raise KeyNotFoundError(
            f"No keyring key for identity {ident!r} (expected {path}). "
            f"Create one explicitly with `nova promote keygen --identity {ident}`, "
            "then have the signer trust list bind its public key to that identity."
        )
    else:
        private_key = Ed25519PrivateKey.generate()
        path.parent.mkdir(parents=True, exist_ok=True)
        pem = private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        try:
            # 0600 from the first byte; exclusive, so two first callers never
            # overwrite each other's key (the loser reads the winner's).
            write_private_file(path, pem, exclusive=True)
        except FileExistsError:
            private_key = _load_key(path)
    fp = _fingerprint(private_key.public_key())
    return private_key, fp


def _load_key(path: Path) -> Ed25519PrivateKey:
    private_key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    if not isinstance(private_key, Ed25519PrivateKey):
        raise ValueError(f"Key at {path} is not Ed25519")
    return private_key


def sign_payload(private_key: Ed25519PrivateKey, payload: bytes) -> str:
    """Return base64url-encoded Ed25519 signature over *payload*."""
    sig = private_key.sign(payload)
    return base64.urlsafe_b64encode(sig).rstrip(b"=").decode()


def verify_sig(public_key: Ed25519PublicKey, sig_b64: str, payload: bytes) -> bool:
    """Return True if *sig_b64* is a valid Ed25519 signature over *payload*."""
    padding = "=" * (-len(sig_b64) % 4)
    try:
        sig_bytes = base64.urlsafe_b64decode(sig_b64 + padding)
        public_key.verify(sig_bytes, payload)
        return True
    except Exception:
        return False


def canonical_payload(proposal_id: str, name: str, version: str, to_status: str) -> bytes:
    return f"{proposal_id}|{name}@{version}:{to_status}".encode()
