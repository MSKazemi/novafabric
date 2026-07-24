"""Offline token subsystem for airgapped / SLURM deployments.

ADR-0018: ed25519 keypair; JWT signing without an OIDC provider.

Schema
------
token_audit table (SQLite) — created in the same DB as the asset registry:

    CREATE TABLE IF NOT EXISTS token_audit (
        token_id   TEXT PRIMARY KEY,
        subject    TEXT NOT NULL,
        roles      TEXT NOT NULL,   -- JSON array
        issued_at  TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        revoked    INTEGER NOT NULL DEFAULT 0
    )
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
    load_pem_private_key,
    load_pem_public_key,
)

from novafabric.server.auth import AuthContext

logger = logging.getLogger(__name__)

_ALGORITHM = "EdDSA"

# ---------------------------------------------------------------------------
# Keypair generation
# ---------------------------------------------------------------------------


def generate_keypair(path: Path) -> tuple[Path, Path]:
    """Generate an ed25519 keypair.

    Writes:
    - private key PEM to *path* (mode 0o600)
    - public  key PEM to *path*.with_suffix('.pub')

    Returns (private_path, public_path).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    private_key = Ed25519PrivateKey.generate()
    priv_pem = private_key.private_bytes(
        encoding=Encoding.PEM,
        format=PrivateFormat.PKCS8,
        encryption_algorithm=NoEncryption(),
    )
    pub_pem = private_key.public_key().public_bytes(
        encoding=Encoding.PEM,
        format=PublicFormat.SubjectPublicKeyInfo,
    )
    # Create the private key with 0600 from the outset — a write-then-chmod
    # sequence leaves a window where the key is readable at the umask default.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(priv_pem)
    path.chmod(0o600)  # idempotent: also normalises a pre-existing file
    pub_path = path.with_suffix(".pub")
    pub_path.write_bytes(pub_pem)
    logger.info("Generated ed25519 keypair at %s", path)
    return path, pub_path


# ---------------------------------------------------------------------------
# Token audit database helpers
# ---------------------------------------------------------------------------

_DDL = """
CREATE TABLE IF NOT EXISTS token_audit (
    token_id   TEXT PRIMARY KEY,
    subject    TEXT NOT NULL,
    roles      TEXT NOT NULL,
    issued_at  TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked    INTEGER NOT NULL DEFAULT 0
)
"""


def _get_conn(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute(_DDL)
    conn.commit()
    return conn


def _token_db_path(key_path: Path) -> Path:
    """Derive the SQLite path for offline token audit records.

    Stored alongside the private key as ``<key_name>-tokens.db``.
    """
    stem = key_path.stem
    return key_path.parent / f"{stem}-tokens.db"


# ---------------------------------------------------------------------------
# Issue / revoke / verify
# ---------------------------------------------------------------------------


def issue_token(
    subject: str,
    roles: list[str],
    expires_in_days: int,
    key_path: Path,
    *,
    db_path: Path | None = None,
) -> str:
    """Sign a JWT with the ed25519 private key at *key_path*.

    The token record is persisted in the audit DB adjacent to the key.
    Returns the encoded JWT string.
    """
    priv_key = _load_private_key(key_path)

    token_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(days=expires_in_days)

    payload = {
        "jti": token_id,
        "sub": subject,
        "nova_roles": roles,
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
        "iss": "nova-offline",
    }
    token = jwt.encode(payload, priv_key, algorithm=_ALGORITHM)

    # Persist audit record
    _db = db_path or _token_db_path(key_path)
    conn = _get_conn(_db)
    try:
        conn.execute(
            """
            INSERT INTO token_audit
                (token_id, subject, roles, issued_at, expires_at, revoked)
            VALUES (?, ?, ?, ?, ?, 0)
            """,
            (
                token_id,
                subject,
                json.dumps(roles),
                now.isoformat(),
                expires_at.isoformat(),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    logger.info("Issued offline token %s for %s (expires %s)", token_id, subject, expires_at.date())
    return token


def revoke_token(
    token_id: str,
    key_path: Path,
    *,
    db_path: Path | None = None,
) -> None:
    """Mark *token_id* as revoked in the audit DB.

    Raises ``KeyError`` if the token_id is not found.
    """
    _db = db_path or _token_db_path(key_path)
    conn = _get_conn(_db)
    try:
        cur = conn.execute(
            "UPDATE token_audit SET revoked = 1 WHERE token_id = ?", (token_id,)
        )
        conn.commit()
        if cur.rowcount == 0:
            raise KeyError(f"Token '{token_id}' not found in audit log")
    finally:
        conn.close()
    logger.info("Revoked offline token %s", token_id)


def verify_offline_token(
    token: str,
    key_path: Path,
    *,
    db_path: Path | None = None,
) -> AuthContext:
    """Validate *token* signed by the ed25519 key at *key_path*.

    Checks:
    - ed25519 signature (via public key derived from private key or .pub file)
    - expiry (``exp`` claim)
    - not revoked in the audit DB

    Returns AuthContext on success; raises ``jwt.exceptions.PyJWTError`` or
    ``PermissionError`` on failure.
    """
    pub_key = _load_public_key(key_path)

    try:
        payload = jwt.decode(
            token,
            pub_key,
            algorithms=[_ALGORITHM],
            options={"require": ["exp", "sub", "jti"]},
        )
    except jwt.ExpiredSignatureError:
        raise jwt.ExpiredSignatureError("Offline token has expired")
    except jwt.exceptions.PyJWTError:
        raise

    token_id: str = payload["jti"]

    # Check revocation
    _db = db_path or _token_db_path(key_path)
    conn = _get_conn(_db)
    try:
        row = conn.execute(
            "SELECT revoked FROM token_audit WHERE token_id = ?", (token_id,)
        ).fetchone()
    finally:
        conn.close()

    if row is not None and int(row["revoked"]):
        raise PermissionError(f"Offline token '{token_id}' has been revoked")

    subject: str = payload.get("sub", "")
    raw_roles: object = payload.get("nova_roles") or payload.get("roles") or []
    roles: list[str] = list(raw_roles) if isinstance(raw_roles, list) else []
    return AuthContext(subject=subject, roles=roles)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _load_private_key(key_path: Path) -> Ed25519PrivateKey:
    pem = key_path.read_bytes()
    key = load_pem_private_key(pem, password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise TypeError(f"Expected ed25519 private key, got {type(key).__name__}")
    return key


def _load_public_key(key_path: Path) -> Ed25519PublicKey:
    """Load the ed25519 public key.

    If *key_path* is the private key path, derives the public key from it.
    If *key_path*.with_suffix('.pub') exists, loads from that file instead.
    """
    pub_path = key_path.with_suffix(".pub")
    if pub_path.exists():
        raw = load_pem_public_key(pub_path.read_bytes())
        if not isinstance(raw, Ed25519PublicKey):
            raise TypeError(f"Expected ed25519 public key, got {type(raw).__name__}")
        return raw
    # Fall back to deriving from private key
    return _load_private_key(key_path).public_key()
