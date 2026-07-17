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

Every create/revoke/rotate appends to the hash-chained audit log
(:class:`novafabric.audit.AuditLog`) with the ``key_id`` and actor — never
secret material or hashes (ADR-0193 D5).

Slice 2 (ADR-0193) adds: ``rotate_key`` (successor with identical bindings +
bounded overlap window, then predecessor auto-revokes at verify time), coarse
``last_used_at`` tracking (at most one write per configurable interval), and
the columns ``last_used_at`` / ``rotated_to`` / ``rotate_expires_at`` (added
in-place to pre-slice-2 databases). The ``/v0/api-keys`` REST resource lives in
``server/routes/api_keys.py``. See ``design/spec/api-keys-v0.md``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
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

# ADR-0193 D3/D4 — bounded, configurable via env; overridable per call for
# tests. The rotate overlap window is the period during which BOTH the
# predecessor and its successor verify; last-used tracking is deliberately
# coarse (at most one write per interval per key), so verification stays
# read-mostly on the hot path.
ROTATE_OVERLAP_ENV = "NOVA_API_KEY_ROTATE_OVERLAP_S"
LASTUSED_INTERVAL_ENV = "NOVA_API_KEY_LASTUSED_INTERVAL_S"
_DEFAULT_ROTATE_OVERLAP_S = 86_400  # 24h — a full day for agents to swap
_DEFAULT_LASTUSED_INTERVAL_S = 86_400  # daily

# Fresh DBs get the full slice-2 column set; deployed pre-slice-2 DBs are
# migrated in place by _ensure_schema (additive ALTER TABLE ADD COLUMN).
_DDL = """
CREATE TABLE IF NOT EXISTS api_keys (
    key_id            TEXT PRIMARY KEY,
    secret_sha256     TEXT NOT NULL,
    owner             TEXT NOT NULL,
    roles             TEXT NOT NULL,
    workspace         TEXT,
    expires_at        TEXT,
    created_at        TEXT NOT NULL,
    revoked_at        TEXT,
    last_used_at      TEXT,
    rotated_to        TEXT,
    rotate_expires_at TEXT
)
"""

# Additive columns introduced by slice 2 (name → column type).
_SLICE2_COLUMNS = {
    "last_used_at": "TEXT",
    "rotated_to": "TEXT",
    "rotate_expires_at": "TEXT",
}

_LIST_COLUMNS = (
    "key_id, owner, roles, workspace, expires_at, created_at, revoked_at, "
    "last_used_at, rotated_to, rotate_expires_at"
)


class InvalidRoleError(ValueError):
    """Raised when a requested role set is empty or outside the RBAC vocabulary."""


class UnknownKeyError(KeyError):
    """Raised when a ``key_id`` is not present in the store."""


class RevokedKeyError(ValueError):
    """Raised when a lifecycle op (e.g. rotate) targets an already-revoked key."""


# ---------------------------------------------------------------------------
# Storage helpers (rbac_store conventions: registry DB, per-call connection)
# ---------------------------------------------------------------------------


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the table if absent and add any missing slice-2 columns.

    Additive, idempotent migration: deployed pre-slice-2 databases carry the
    original eight columns; each new column is appended with ``ALTER TABLE ADD
    COLUMN`` only when not already present.
    """
    conn.execute(_DDL)
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(api_keys)")}
    for name, col_type in _SLICE2_COLUMNS.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE api_keys ADD COLUMN {name} {col_type}")
    conn.commit()


def _get_conn(db_path: Path | None) -> sqlite3.Connection:
    from novafabric.registry.store import get_connection, get_db_path

    resolved = db_path or get_db_path()
    conn = get_connection(resolved)
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    return conn


def _rotate_overlap_seconds(override: int | None) -> int:
    if override is not None:
        return max(0, override)
    raw = os.environ.get(ROTATE_OVERLAP_ENV)
    if raw is None:
        return _DEFAULT_ROTATE_OVERLAP_S
    try:
        return max(0, int(raw))
    except ValueError:
        return _DEFAULT_ROTATE_OVERLAP_S


def _lastused_interval_seconds(override: int | None) -> int:
    if override is not None:
        return max(0, override)
    raw = os.environ.get(LASTUSED_INTERVAL_ENV)
    if raw is None:
        return _DEFAULT_LASTUSED_INTERVAL_S
    try:
        return max(0, int(raw))
    except ValueError:
        return _DEFAULT_LASTUSED_INTERVAL_S


def key_status(row: dict[str, Any], *, now: datetime | None = None) -> str:
    """Derive a coarse lifecycle status from a metadata row (never a secret).

    Returns one of ``active`` / ``revoked`` / ``expired``. A rotated
    predecessor whose overlap window has elapsed is reported as ``revoked``
    (ADR-0193 D3: the window expiry is an auto-revoke).
    """
    now = now or datetime.now(timezone.utc)
    if row.get("revoked_at"):
        return "revoked"
    rotate_expires_at = row.get("rotate_expires_at")
    if rotate_expires_at and now >= datetime.fromisoformat(rotate_expires_at):
        return "revoked"
    expires_at = row.get("expires_at")
    if expires_at and now >= datetime.fromisoformat(expires_at):
        return "expired"
    return "active"


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
# Lifecycle: create / verify / list / revoke / rotate
# ---------------------------------------------------------------------------


def _new_secret() -> tuple[str, str]:
    """Return ``(secret, sha256_hex)`` for a fresh high-entropy key secret."""
    secret = secrets.token_urlsafe(_SECRET_BYTES)
    return secret, hashlib.sha256(secret.encode()).hexdigest()


def _insert_key(
    conn: sqlite3.Connection,
    *,
    digest: str,
    owner: str,
    roles_json: str,
    workspace: str | None,
    expires_at: str | None,
    created_at: str,
) -> str:
    """Insert a new key row (collision-retried) and return its ``key_id``."""
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
                (key_id, digest, owner, roles_json, workspace, expires_at, created_at),
            )
            conn.commit()
            return key_id
        except sqlite3.IntegrityError:  # pragma: no cover — 48-bit collision
            continue
    raise RuntimeError("could not allocate a unique api-key id")  # pragma: no cover


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

    secret, digest = _new_secret()

    conn = _get_conn(db_path)
    try:
        key_id = _insert_key(
            conn,
            digest=digest,
            owner=owner,
            roles_json=json.dumps(role_list),
            workspace=workspace,
            expires_at=expires_at,
            created_at=now.isoformat(),
        )
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


def _maybe_touch_last_used(
    conn: sqlite3.Connection,
    key_id: str,
    stored_last_used: str | None,
    now: datetime,
    interval_seconds: int,
) -> None:
    """Coarsely update ``last_used_at`` at most once per interval (ADR-0193 D4).

    A no-op inside the interval keeps verification read-mostly on the hot path.
    """
    if stored_last_used is not None:
        last = datetime.fromisoformat(stored_last_used)
        if (now - last).total_seconds() < interval_seconds:
            return
    conn.execute(
        "UPDATE api_keys SET last_used_at = ? WHERE key_id = ?",
        (now.isoformat(), key_id),
    )
    conn.commit()


def verify_key(
    key: str,
    *,
    db_path: Path | None = None,
    lastused_interval_seconds: int | None = None,
) -> AuthContext | None:
    """Resolve a full key string to its principal + roles, or None.

    None (never an exception) for: malformed keys, unknown key_ids, secret
    mismatch, revoked keys, expired keys, and rotated predecessors whose
    overlap window has elapsed (ADR-0193 D3). The secret comparison is
    constant-time (``hmac.compare_digest`` over sha256 hex digests).

    On success, ``last_used_at`` is refreshed at most once per
    ``lastused_interval_seconds`` (default: ``NOVA_API_KEY_LASTUSED_INTERVAL_S``
    → daily), so a busy key does not incur a write per request (ADR-0193 D4).
    """
    parsed = parse_key(key)
    if parsed is None:
        return None
    key_id, secret = parsed

    conn = _get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT secret_sha256, owner, roles, expires_at, revoked_at, "
            "last_used_at, rotate_expires_at "
            "FROM api_keys WHERE key_id = ?",
            (key_id,),
        ).fetchone()

        presented = hashlib.sha256(secret.encode()).hexdigest()
        if row is None:
            # Equalize work for unknown key_ids; the result is always a reject.
            hmac.compare_digest(presented, presented)
            return None
        if not hmac.compare_digest(presented, row["secret_sha256"]):
            return None
        if row["revoked_at"] is not None:
            return None
        now = datetime.now(timezone.utc)
        if row["expires_at"] is not None and now >= datetime.fromisoformat(
            row["expires_at"]
        ):
            return None
        # Rotated predecessor: valid only until the overlap window elapses.
        if row["rotate_expires_at"] is not None and now >= datetime.fromisoformat(
            row["rotate_expires_at"]
        ):
            return None

        _maybe_touch_last_used(
            conn,
            key_id,
            row["last_used_at"],
            now,
            _lastused_interval_seconds(lastused_interval_seconds),
        )
        roles = json.loads(row["roles"])
        return AuthContext(subject=row["owner"], roles=list(roles))
    finally:
        conn.close()


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


def rotate_key(
    key_id: str,
    *,
    actor: str,
    overlap_seconds: int | None = None,
    db_path: Path | None = None,
    audit_log_path: Path | None = None,
) -> tuple[str, dict[str, Any]]:
    """Rotate *key_id*: mint a successor with identical bindings (ADR-0193 D3).

    Returns ``(successor_full_key, successor_record)`` — the successor key is
    returned ONCE. Both predecessor and successor verify during the bounded
    ``overlap_seconds`` window (default: ``NOVA_API_KEY_ROTATE_OVERLAP_S`` →
    24h); after it elapses the predecessor is auto-revoked at verify time (no
    background job). Raises :class:`UnknownKeyError` if the predecessor is
    absent and :class:`RevokedKeyError` if it is already revoked.
    """
    overlap = _rotate_overlap_seconds(overlap_seconds)
    now = datetime.now(timezone.utc)
    rotate_expires_at = (now + timedelta(seconds=overlap)).isoformat()
    secret, digest = _new_secret()

    conn = _get_conn(db_path)
    try:
        pred = conn.execute(
            "SELECT owner, roles, workspace, expires_at, revoked_at "
            "FROM api_keys WHERE key_id = ?",
            (key_id,),
        ).fetchone()
        if pred is None:
            raise UnknownKeyError(f"API key '{key_id}' not found")
        if pred["revoked_at"] is not None:
            raise RevokedKeyError(f"API key '{key_id}' is revoked; cannot rotate")

        successor_id = _insert_key(
            conn,
            digest=digest,
            owner=pred["owner"],
            roles_json=pred["roles"],
            workspace=pred["workspace"],
            expires_at=pred["expires_at"],
            created_at=now.isoformat(),
        )
        conn.execute(
            "UPDATE api_keys SET rotated_to = ?, rotate_expires_at = ? "
            "WHERE key_id = ?",
            (successor_id, rotate_expires_at, key_id),
        )
        conn.commit()
        role_list = list(json.loads(pred["roles"]))
        record: dict[str, Any] = {
            "key_id": successor_id,
            "owner": pred["owner"],
            "roles": role_list,
            "workspace": pred["workspace"],
            "expires_at": pred["expires_at"],
            "created_at": now.isoformat(),
            "revoked_at": None,
        }
    finally:
        conn.close()

    # Successor is a first-class created key; predecessor records the rotation.
    _audit(
        "api_key.create",
        actor,
        successor_id,
        {
            "owner": record["owner"],
            "roles": role_list,
            "workspace": record["workspace"],
            "expires_at": record["expires_at"],
            "rotated_from": key_id,
        },
        audit_log_path,
    )
    _audit(
        "api_key.rotate",
        actor,
        key_id,
        {"rotated_to": successor_id, "rotate_expires_at": rotate_expires_at},
        audit_log_path,
    )
    logger.info(
        "Rotated API key %s → %s (overlap %ds)", key_id, successor_id, overlap
    )

    record["rotate_overlap_seconds"] = overlap
    record["rotate_expires_at"] = rotate_expires_at
    return f"{KEY_PREFIX}{successor_id}_{secret}", record
