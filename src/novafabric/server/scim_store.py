"""SQLite store for SCIM 2.0 provisioning (server mode only).

ADR-0139 / spec ``design/spec/scim-provisioning-v0.md``: stored SCIM User
subset (PII-minimal, closed) plus the append-only provisioning audit log.

Additive tables in the existing registry SQLite database — no existing table
is altered. Local mode never touches this module: it is only reachable via
the ``/scim/v2`` routes, which are inert unless SCIM is explicitly enabled.

Schema:
    CREATE TABLE IF NOT EXISTS scim_users (
        id            TEXT PRIMARY KEY,
        user_name     TEXT NOT NULL UNIQUE,
        external_id   TEXT,
        display_name  TEXT,
        active        INTEGER NOT NULL,
        emails        TEXT,           -- JSON array of {value, primary}
        created       TEXT NOT NULL,  -- RFC 3339 UTC
        last_modified TEXT NOT NULL
    )
    CREATE TABLE IF NOT EXISTS scim_audit_events (
        event_id        TEXT PRIMARY KEY,  -- ULID, append-only
        occurred_at     TEXT NOT NULL,     -- RFC 3339 UTC
        actor           TEXT NOT NULL,     -- provisioning-token identity
        operation       TEXT NOT NULL,     -- user.create / user.deactivate / ...
        resource_type   TEXT NOT NULL,     -- User | Group
        subject         TEXT NOT NULL,     -- affected userName
        roles_before    TEXT NOT NULL,     -- JSON array of role strings
        roles_after     TEXT NOT NULL,
        scim_request_id TEXT
    )
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from novafabric.capture._ulid import new_ulid

_DDL = """
CREATE TABLE IF NOT EXISTS scim_users (
    id            TEXT PRIMARY KEY,
    user_name     TEXT NOT NULL UNIQUE,
    external_id   TEXT,
    display_name  TEXT,
    active        INTEGER NOT NULL,
    emails        TEXT,
    created       TEXT NOT NULL,
    last_modified TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scim_audit_events (
    event_id        TEXT PRIMARY KEY,
    occurred_at     TEXT NOT NULL,
    actor           TEXT NOT NULL,
    operation       TEXT NOT NULL,
    resource_type   TEXT NOT NULL,
    subject         TEXT NOT NULL,
    roles_before    TEXT NOT NULL,
    roles_after     TEXT NOT NULL,
    scim_request_id TEXT
);
"""

#: The only attributes persisted for a filter query (least-surface subset).
FILTERABLE_ATTRIBUTES = ("userName", "externalId", "active")


class DuplicateUserNameError(Exception):
    """Raised when a create would violate the unique ``userName`` constraint."""


@dataclass
class ScimUser:
    """The stored, PII-minimal SCIM User subset (spec: closed schema)."""

    id: str
    user_name: str
    active: bool
    external_id: str | None = None
    display_name: str | None = None
    emails: list[dict[str, Any]] = field(default_factory=list)
    created: str = ""
    last_modified: str = ""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_conn(db_path: Path | None) -> sqlite3.Connection:
    from novafabric.registry.store import get_connection, get_db_path

    resolved = db_path or get_db_path()
    conn = get_connection(resolved)
    conn.row_factory = sqlite3.Row
    conn.executescript(_DDL)
    conn.commit()
    return conn


def _row_to_user(row: sqlite3.Row) -> ScimUser:
    return ScimUser(
        id=row["id"],
        user_name=row["user_name"],
        active=bool(row["active"]),
        external_id=row["external_id"],
        display_name=row["display_name"],
        emails=json.loads(row["emails"]) if row["emails"] else [],
        created=row["created"],
        last_modified=row["last_modified"],
    )


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


def create_user(
    user_name: str,
    *,
    active: bool = True,
    external_id: str | None = None,
    display_name: str | None = None,
    emails: list[dict[str, Any]] | None = None,
    db_path: Path | None = None,
) -> ScimUser:
    """Insert a new SCIM user. Raises DuplicateUserNameError on userName clash."""
    now = _now()
    user = ScimUser(
        id=new_ulid(),
        user_name=user_name,
        active=active,
        external_id=external_id,
        display_name=display_name,
        emails=emails or [],
        created=now,
        last_modified=now,
    )
    conn = _get_conn(db_path)
    try:
        conn.execute(
            """
            INSERT INTO scim_users
                (id, user_name, external_id, display_name, active, emails,
                 created, last_modified)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user.id,
                user.user_name,
                user.external_id,
                user.display_name,
                int(user.active),
                json.dumps(user.emails),
                user.created,
                user.last_modified,
            ),
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        raise DuplicateUserNameError(
            f"a SCIM user with userName {user_name!r} already exists"
        ) from exc
    finally:
        conn.close()
    return user


def get_user(user_id: str, *, db_path: Path | None = None) -> ScimUser | None:
    """Return the user with *user_id*, or None."""
    conn = _get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM scim_users WHERE id = ?", (user_id,)
        ).fetchone()
        return _row_to_user(row) if row else None
    finally:
        conn.close()


def list_users(
    *,
    filter_attr: str | None = None,
    filter_value: str | bool | None = None,
    start_index: int = 1,
    count: int | None = None,
    db_path: Path | None = None,
) -> tuple[int, list[ScimUser]]:
    """Return ``(total_results, page)`` with optional ``eq`` filter + pagination.

    *filter_attr* must be one of FILTERABLE_ATTRIBUTES (caller-validated).
    *start_index* is 1-based per RFC 7644 §3.4.2.4.
    """
    where = ""
    params: list[Any] = []
    if filter_attr is not None:
        column = {
            "userName": "user_name",
            "externalId": "external_id",
            "active": "active",
        }[filter_attr]
        where = f" WHERE {column} = ?"
        params.append(int(filter_value) if isinstance(filter_value, bool) else filter_value)

    conn = _get_conn(db_path)
    try:
        total = conn.execute(
            f"SELECT COUNT(*) AS c FROM scim_users{where}", params
        ).fetchone()["c"]
        sql = f"SELECT * FROM scim_users{where} ORDER BY created, id"
        page_params = list(params)
        limit = -1 if count is None else max(count, 0)
        sql += " LIMIT ? OFFSET ?"
        page_params.extend([limit, max(start_index - 1, 0)])
        rows = conn.execute(sql, page_params).fetchall()
        return total, [_row_to_user(r) for r in rows]
    finally:
        conn.close()


def update_user(
    user_id: str,
    *,
    changes: dict[str, Any],
    db_path: Path | None = None,
) -> ScimUser | None:
    """Apply *changes* (keys: active, display_name, external_id) to a user.

    Returns the updated user, or None if *user_id* does not exist.
    """
    allowed = {"active", "display_name", "external_id"}
    unknown = set(changes) - allowed
    if unknown:
        raise ValueError(f"unsupported scim_users columns: {sorted(unknown)}")
    if not changes:
        return get_user(user_id, db_path=db_path)
    sets = ", ".join(f"{key} = ?" for key in changes)
    params: list[Any] = [
        int(v) if isinstance(v, bool) else v for v in changes.values()
    ]
    conn = _get_conn(db_path)
    try:
        cursor = conn.execute(
            f"UPDATE scim_users SET {sets}, last_modified = ? WHERE id = ?",
            [*params, _now(), user_id],
        )
        conn.commit()
        if cursor.rowcount == 0:
            return None
    finally:
        conn.close()
    return get_user(user_id, db_path=db_path)


def delete_user(user_id: str, *, db_path: Path | None = None) -> bool:
    """Hard-delete a user row. Returns True if a row was deleted."""
    conn = _get_conn(db_path)
    try:
        cursor = conn.execute("DELETE FROM scim_users WHERE id = ?", (user_id,))
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Append-only provisioning audit log (ADR-0139 D5)
# ---------------------------------------------------------------------------


def append_audit_event(
    *,
    actor: str,
    operation: str,
    resource_type: str,
    subject: str,
    roles_before: list[str],
    roles_after: list[str],
    scim_request_id: str | None = None,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """Append one immutable provisioning audit event and return it."""
    event: dict[str, Any] = {
        "event_id": new_ulid(),
        "occurred_at": _now(),
        "actor": actor,
        "operation": operation,
        "resource_type": resource_type,
        "subject": subject,
        "roles_before": roles_before,
        "roles_after": roles_after,
        "scim_request_id": scim_request_id,
    }
    conn = _get_conn(db_path)
    try:
        conn.execute(
            """
            INSERT INTO scim_audit_events
                (event_id, occurred_at, actor, operation, resource_type,
                 subject, roles_before, roles_after, scim_request_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event["event_id"],
                event["occurred_at"],
                event["actor"],
                event["operation"],
                event["resource_type"],
                event["subject"],
                json.dumps(roles_before),
                json.dumps(roles_after),
                scim_request_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return event


def list_audit_events(
    *,
    subject: str | None = None,
    db_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Return provisioning audit events (oldest first), optionally by subject."""
    conn = _get_conn(db_path)
    try:
        if subject is not None:
            rows = conn.execute(
                "SELECT * FROM scim_audit_events WHERE subject = ?"
                " ORDER BY occurred_at, event_id",
                (subject,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM scim_audit_events ORDER BY occurred_at, event_id"
            ).fetchall()
        events: list[dict[str, Any]] = []
        for row in rows:
            record = dict(row)
            record["roles_before"] = json.loads(record["roles_before"])
            record["roles_after"] = json.loads(record["roles_after"])
            events.append(record)
        return events
    finally:
        conn.close()
