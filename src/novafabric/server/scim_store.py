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
CREATE TABLE IF NOT EXISTS scim_groups (
    id            TEXT PRIMARY KEY,
    display_name  TEXT NOT NULL,
    external_id   TEXT,
    created       TEXT NOT NULL,
    last_modified TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scim_group_members (
    group_id TEXT NOT NULL,
    user_id  TEXT NOT NULL,
    PRIMARY KEY (group_id, user_id)
);
CREATE TABLE IF NOT EXISTS scim_group_role_grants (
    subject TEXT NOT NULL,   -- RBAC subject (userName) SCIM granted a role to
    role    TEXT NOT NULL,
    PRIMARY KEY (subject, role)
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


# ---------------------------------------------------------------------------
# SCIM Groups (ADR-0139 D3) — storage + membership
# ---------------------------------------------------------------------------


@dataclass
class ScimGroup:
    id: str
    display_name: str
    external_id: str | None
    members: list[str]  # SCIM User ids
    created: str
    last_modified: str


def _group_member_ids(conn: sqlite3.Connection, group_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT user_id FROM scim_group_members WHERE group_id = ? ORDER BY user_id",
        (group_id,),
    ).fetchall()
    return [r["user_id"] for r in rows]


def create_group(
    display_name: str,
    *,
    external_id: str | None = None,
    member_ids: list[str] | None = None,
    db_path: Path | None = None,
) -> ScimGroup:
    """Create a SCIM Group with an optional initial membership."""
    group_id = new_ulid()
    now = _now()
    conn = _get_conn(db_path)
    try:
        conn.execute(
            "INSERT INTO scim_groups (id, display_name, external_id, created, last_modified)"
            " VALUES (?, ?, ?, ?, ?)",
            (group_id, display_name, external_id, now, now),
        )
        for uid in member_ids or []:
            conn.execute(
                "INSERT OR IGNORE INTO scim_group_members (group_id, user_id) VALUES (?, ?)",
                (group_id, uid),
            )
        conn.commit()
        members = _group_member_ids(conn, group_id)
    finally:
        conn.close()
    return ScimGroup(group_id, display_name, external_id, members, now, now)


def get_group(group_id: str, *, db_path: Path | None = None) -> ScimGroup | None:
    conn = _get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM scim_groups WHERE id = ?", (group_id,)
        ).fetchone()
        if row is None:
            return None
        members = _group_member_ids(conn, group_id)
    finally:
        conn.close()
    return ScimGroup(
        row["id"], row["display_name"], row["external_id"], members,
        row["created"], row["last_modified"],
    )


def list_groups(*, db_path: Path | None = None) -> list[ScimGroup]:
    conn = _get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM scim_groups ORDER BY display_name"
        ).fetchall()
        return [
            ScimGroup(
                r["id"], r["display_name"], r["external_id"],
                _group_member_ids(conn, r["id"]), r["created"], r["last_modified"],
            )
            for r in rows
        ]
    finally:
        conn.close()


def replace_group(
    group_id: str,
    *,
    display_name: str,
    external_id: str | None = None,
    member_ids: list[str] | None = None,
    db_path: Path | None = None,
) -> ScimGroup | None:
    """RFC 7644 §3.5.1 full replace: displayName, externalId and membership.

    All mutations happen in one transaction (single connection, one commit), so
    a PUT never leaves a group half-replaced. Returns the updated group, or
    None if *group_id* does not exist (nothing is mutated in that case).
    """
    now = _now()
    conn = _get_conn(db_path)
    try:
        cursor = conn.execute(
            "UPDATE scim_groups SET display_name = ?, external_id = ?,"
            " last_modified = ? WHERE id = ?",
            (display_name, external_id, now, group_id),
        )
        if cursor.rowcount == 0:
            conn.rollback()
            return None
        conn.execute(
            "DELETE FROM scim_group_members WHERE group_id = ?", (group_id,)
        )
        for uid in member_ids or []:
            conn.execute(
                "INSERT OR IGNORE INTO scim_group_members (group_id, user_id) VALUES (?, ?)",
                (group_id, uid),
            )
        conn.commit()
        members = _group_member_ids(conn, group_id)
        row = conn.execute(
            "SELECT created FROM scim_groups WHERE id = ?", (group_id,)
        ).fetchone()
    finally:
        conn.close()
    return ScimGroup(group_id, display_name, external_id, members, row["created"], now)


def add_group_member(group_id: str, user_id: str, *, db_path: Path | None = None) -> None:
    conn = _get_conn(db_path)
    try:
        conn.execute(
            "INSERT OR IGNORE INTO scim_group_members (group_id, user_id) VALUES (?, ?)",
            (group_id, user_id),
        )
        conn.execute(
            "UPDATE scim_groups SET last_modified = ? WHERE id = ?", (_now(), group_id)
        )
        conn.commit()
    finally:
        conn.close()


def remove_group_member(group_id: str, user_id: str, *, db_path: Path | None = None) -> None:
    conn = _get_conn(db_path)
    try:
        conn.execute(
            "DELETE FROM scim_group_members WHERE group_id = ? AND user_id = ?",
            (group_id, user_id),
        )
        conn.execute(
            "UPDATE scim_groups SET last_modified = ? WHERE id = ?", (_now(), group_id)
        )
        conn.commit()
    finally:
        conn.close()


def delete_group(group_id: str, *, db_path: Path | None = None) -> bool:
    conn = _get_conn(db_path)
    try:
        cur = conn.execute("DELETE FROM scim_groups WHERE id = ?", (group_id,))
        conn.execute("DELETE FROM scim_group_members WHERE group_id = ?", (group_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def group_display_names_for_user(user_id: str, *, db_path: Path | None = None) -> list[str]:
    """Return the displayNames of every group the given SCIM User id belongs to."""
    conn = _get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT g.display_name FROM scim_groups g "
            "JOIN scim_group_members m ON m.group_id = g.id WHERE m.user_id = ?",
            (user_id,),
        ).fetchall()
        return [r["display_name"] for r in rows]
    finally:
        conn.close()


#: ``assigned_by`` provenance marker for RBAC rows SCIM created (ADR-0190).
SCIM_ROLE_SOURCE = "scim:group"


@dataclass(frozen=True)
class ReconcileResult:
    """Effect of reconciling a subject's SCIM-owned roles to a desired set."""

    before: set[str]
    after: set[str]
    granted: set[str]
    revoked: set[str]


def _scim_owned_roles(subject: str, *, db_path: Path | None) -> set[str]:
    conn = _get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT role FROM scim_group_role_grants WHERE subject = ?", (subject,)
        ).fetchall()
        return {row["role"] for row in rows}
    finally:
        conn.close()


def _record_scim_grant(subject: str, role: str, *, db_path: Path | None) -> None:
    conn = _get_conn(db_path)
    try:
        conn.execute(
            "INSERT OR IGNORE INTO scim_group_role_grants (subject, role) VALUES (?, ?)",
            (subject, role),
        )
        conn.commit()
    finally:
        conn.close()


def _drop_scim_grant(subject: str, role: str, *, db_path: Path | None) -> None:
    conn = _get_conn(db_path)
    try:
        conn.execute(
            "DELETE FROM scim_group_role_grants WHERE subject = ? AND role = ?",
            (subject, role),
        )
        conn.commit()
    finally:
        conn.close()


def reconcile_subject_roles(
    subject: str,
    desired_roles: set[str],
    actor: str,
    *,
    scim_request_id: str | None = None,
    db_path: Path | None = None,
) -> ReconcileResult:
    """Make the subject's SCIM-owned RBAC roles equal ``desired_roles`` (ADR-0190).

    Only rows SCIM owns (``assigned_by == "scim:group"``) are ever granted or
    revoked; a role an operator/OIDC already owns is never seized or removed. The
    ADR-0060 last-admin guard fires on revoke (``LastAdminError`` propagates with
    no partial mutation of that role). One append-only audit event is written when
    the SCIM-owned role set changes.
    """
    from novafabric.server import rbac_store

    prev = _scim_owned_roles(subject, db_path=db_path)
    desired = set(desired_roles)
    granted: set[str] = set()
    revoked: set[str] = set()

    for role in sorted(desired - prev):
        existing = rbac_store.get_assigned_by(subject, role, db_path=db_path)
        if existing is None:
            rbac_store.assign_role(subject, role, SCIM_ROLE_SOURCE, db_path=db_path)
            _record_scim_grant(subject, role, db_path=db_path)
            granted.add(role)
        elif existing == SCIM_ROLE_SOURCE:
            _record_scim_grant(subject, role, db_path=db_path)
            granted.add(role)
        # else: an operator/OIDC grant owns this role — defer, do not seize it.

    for role in sorted(prev - desired):
        # revoke_role may raise LastAdminError; let it propagate before we drop
        # the intent row, so a refused revoke leaves state unchanged.
        if rbac_store.get_assigned_by(subject, role, db_path=db_path) == SCIM_ROLE_SOURCE:
            rbac_store.revoke_role(subject, role, db_path=db_path)
            revoked.add(role)
        _drop_scim_grant(subject, role, db_path=db_path)

    after = _scim_owned_roles(subject, db_path=db_path)
    if after != prev:
        append_audit_event(
            actor=actor,
            operation="group-role-remap",
            resource_type="Group",
            subject=subject,
            roles_before=sorted(prev),
            roles_after=sorted(after),
            scim_request_id=scim_request_id,
            db_path=db_path,
        )

    return ReconcileResult(before=prev, after=after, granted=granted, revoked=revoked)


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
