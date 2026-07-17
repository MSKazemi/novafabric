"""First-class API keys for the NovaFabric server (ADR-0193 Track 1, experimental).

Key string: ``nvfk_<key_id>_<secret>`` — a fixed recognizable prefix, an
8-character public ``key_id`` for identification in logs and listings, and a
high-entropy secret (``secrets.token_urlsafe(32)``).

Hash-only storage: the store keeps ``sha256(secret)`` (hex) and compares in
constant time via ``hmac.compare_digest``. The full key material is returned
exactly once by :func:`create_key` and is unrecoverable thereafter.

Schema (same registry SQLite DB the ``role_assignments`` /
``token_audit``-style server stores use):

    CREATE TABLE IF NOT EXISTS api_keys (
        key_id        TEXT PRIMARY KEY,
        secret_sha256 TEXT NOT NULL,
        owner         TEXT NOT NULL,
        roles         TEXT NOT NULL,   -- JSON array (reader/writer/admin/auditor)
        workspace     TEXT,            -- optional ADR-0178 workspace scope
        expires_at    TEXT,            -- optional ISO-8601 expiry
        created_at    TEXT NOT NULL,
        revoked_at    TEXT             -- set on revoke; NULL while active
    )

Every create/revoke appends to the hash-chained audit log
(:class:`novafabric.audit.AuditLog`) with the ``key_id`` and actor — never
secret material or hashes (ADR-0193 D5).

Deferred to the next slice (ADR-0193): ``rotate`` (successor + overlap
window), coarse ``last_used_at`` tracking, and the ``/v0/api-keys`` REST
resource. See ``design/spec/api-keys-v0.md``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from novafabric.server.auth import AuthContext

logger = logging.getLogger(__name__)

KEY_PREFIX = "nvfk_"
_KEY_ID_LEN = 8  # token_urlsafe(6) → exactly 8 urlsafe chars
_SECRET_BYTES = 32  # token_urlsafe(32) → 43 urlsafe chars

VALID_ROLES = frozenset({"reader", "writer", "admin", "auditor"})

_DDL = """
CREATE TABLE IF NOT EXISTS api_keys (
    key_id        TEXT PRIMARY KEY,
    secret_sha256 TEXT NOT NULL,
    owner         TEXT NOT NULL,
    roles         TEXT NOT NULL,
    workspace     TEXT,
    expires_at    TEXT,
    created_at    TEXT NOT NULL,
    revoked_at    TEXT
)
"""

_LIST_COLUMNS = (
    "key_id, owner, roles, workspace, expires_at, created_at, revoked_at"
)


class InvalidRoleError(ValueError):
    """Raised when a requested role set is empty or outside the RBAC vocabulary."""


class UnknownKeyError(KeyError):
    """Raised when a ``key_id`` is not present in the store."""


# ---------------------------------------------------------------------------
# Storage helpers (rbac_store conventions: registry DB, per-call connection)
# ---------------------------------------------------------------------------


def _get_conn(db_path: Path | None) -> sqlite3.Connection:
    from novafabric.registry.store import get_connection, get_db_path

    resolved = db_path or get_db_path()
    conn = get_connection(resolved)
    conn.row_factory = sqlite3.Row
    conn.execute(_DDL)
    conn.commit()
    return conn


def _audit(
    event_type_value: str,
    actor: str,
    key_id: str,
    details: dict[str, Any],
    audit_log_path: Path | None,
) -> None:
    """Append one lifecycle entry to the hash-chained audit log (ADR-0193 D5).

    ``details`` must never contain secret material or hashes — callers pass
    only key metadata (owner, roles, scope, expiry).
    """
    from novafabric.audit import AuditEventType, AuditLog, _paths

    path = audit_log_path or _paths.AUDIT_LOG_PATH
    AuditLog(path).append(
        event_type=AuditEventType(event_type_value),
        actor=actor,
        resource_id=key_id,
        details=details,
    )


# ---------------------------------------------------------------------------
# Key format
# ---------------------------------------------------------------------------


def parse_key(key: str) -> tuple[str, str] | None:
    """Split a full key string into ``(key_id, secret)``; None if malformed.

    Parsing is positional (``nvfk_`` + 8 chars + ``_`` + secret) because the
    urlsafe alphabet means the ``key_id`` itself may contain ``_``.
    """
    prefix_end = len(KEY_PREFIX)
    sep_index = prefix_end + _KEY_ID_LEN
    if not key.startswith(KEY_PREFIX) or len(key) <= sep_index + 1:
        return None
    if key[sep_index] != "_":
        return None
    key_id = key[prefix_end:sep_index]
    secret = key[sep_index + 1 :]
    if not key_id or not secret:
        return None
    return key_id, secret


def _validate_roles(roles: list[str]) -> list[str]:
    if not roles:
        raise InvalidRoleError("at least one role is required")
    unknown = sorted(set(roles) - VALID_ROLES)
    if unknown:
        valid = ", ".join(sorted(VALID_ROLES))
        raise InvalidRoleError(f"unknown role(s) {unknown}; choose from: {valid}")
    # De-duplicate, preserve deterministic order.
    return sorted(set(roles))


# ---------------------------------------------------------------------------
# Lifecycle: create / verify / list / revoke
# ---------------------------------------------------------------------------


def create_key(
    owner: str,
    roles: list[str],
    *,
    actor: str,
    workspace: str | None = None,
    expires_in_days: int | None = None,
    db_path: Path | None = None,
    audit_log_path: Path | None = None,
) -> tuple[str, dict[str, Any]]:
    """Create an API key. Returns ``(full_key, record)``.

    The full key string is returned ONCE here and never stored — only the
    sha256 of the secret is persisted. ``record`` is the secret-free metadata
    row (as :func:`list_keys` would return it).
    """
    role_list = _validate_roles(roles)
    now = datetime.now(timezone.utc)
    expires_at: str | None = None
    if expires_in_days is not None:
        expires_at = (now + timedelta(days=expires_in_days)).isoformat()

    secret = secrets.token_urlsafe(_SECRET_BYTES)
    digest = hashlib.sha256(secret.encode()).hexdigest()

    conn = _get_conn(db_path)
    try:
        # token_urlsafe(6) yields exactly 8 chars; retry on the (negligible)
        # chance of a key_id collision — bounded, never unbounded.
        for _ in range(5):
            key_id = secrets.token_urlsafe(6)
            try:
                conn.execute(
                    """
                    INSERT INTO api_keys
                        (key_id, secret_sha256, owner, roles, workspace,
                         expires_at, created_at, revoked_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
                    """,
                    (
                        key_id,
                        digest,
                        owner,
                        json.dumps(role_list),
                        workspace,
                        expires_at,
                        now.isoformat(),
                    ),
                )
                conn.commit()
                break
            except sqlite3.IntegrityError:  # pragma: no cover — 48-bit collision
                continue
        else:  # pragma: no cover
            raise RuntimeError("could not allocate a unique api-key id")
    finally:
        conn.close()

    _audit(
        "api_key.create",
        actor,
        key_id,
        {
            "owner": owner,
            "roles": role_list,
            "workspace": workspace,
            "expires_at": expires_at,
        },
        audit_log_path,
    )
    logger.info("Created API key %s for %s (roles=%s)", key_id, owner, role_list)

    record: dict[str, Any] = {
        "key_id": key_id,
        "owner": owner,
        "roles": role_list,
        "workspace": workspace,
        "expires_at": expires_at,
        "created_at": now.isoformat(),
        "revoked_at": None,
    }
    return f"{KEY_PREFIX}{key_id}_{secret}", record


def verify_key(key: str, *, db_path: Path | None = None) -> AuthContext | None:
    """Resolve a full key string to its principal + roles, or None.

    None (never an exception) for: malformed keys, unknown key_ids, secret
    mismatch, revoked keys, and expired keys. The secret comparison is
    constant-time (``hmac.compare_digest`` over sha256 hex digests).
    """
    parsed = parse_key(key)
    if parsed is None:
        return None
    key_id, secret = parsed

    conn = _get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT secret_sha256, owner, roles, expires_at, revoked_at "
            "FROM api_keys WHERE key_id = ?",
            (key_id,),
        ).fetchone()
    finally:
        conn.close()

    presented = hashlib.sha256(secret.encode()).hexdigest()
    if row is None:
        # Equalize work for unknown key_ids; the result is always a reject.
        hmac.compare_digest(presented, presented)
        return None
    if not hmac.compare_digest(presented, row["secret_sha256"]):
        return None
    if row["revoked_at"] is not None:
        return None
    if row["expires_at"] is not None:
        expires = datetime.fromisoformat(row["expires_at"])
        if datetime.now(timezone.utc) >= expires:
            return None

    roles = json.loads(row["roles"])
    return AuthContext(subject=row["owner"], roles=list(roles))


def list_keys(*, db_path: Path | None = None) -> list[dict[str, Any]]:
    """Return key metadata rows — never secrets, never hashes."""
    conn = _get_conn(db_path)
    try:
        rows = conn.execute(
            f"SELECT {_LIST_COLUMNS} FROM api_keys ORDER BY created_at"
        ).fetchall()
    finally:
        conn.close()
    out: list[dict[str, Any]] = []
    for row in rows:
        d = dict(row)
        d["roles"] = json.loads(d["roles"])
        out.append(d)
    return out


def revoke_key(
    key_id: str,
    *,
    actor: str,
    db_path: Path | None = None,
    audit_log_path: Path | None = None,
) -> None:
    """Revoke *key_id* immediately. Raises :class:`UnknownKeyError` if absent.

    Verification is a DB lookup, so revocation takes effect on the next
    request — no token-style propagation gap (ADR-0193 D3).
    """
    now = datetime.now(timezone.utc).isoformat()
    conn = _get_conn(db_path)
    try:
        cur = conn.execute(
            "UPDATE api_keys SET revoked_at = COALESCE(revoked_at, ?) "
            "WHERE key_id = ?",
            (now, key_id),
        )
        conn.commit()
        if cur.rowcount == 0:
            raise UnknownKeyError(f"API key '{key_id}' not found")
    finally:
        conn.close()

    _audit("api_key.revoke", actor, key_id, {}, audit_log_path)
    logger.info("Revoked API key %s", key_id)
