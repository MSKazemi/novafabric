"""Persistent role assignment store for the NovaFabric server.

ADR-0018: role_assignments table in SQLite.
ADR-0060: revoke + lockout invariant added in v0.14 for the HTTP role-management surface.

Schema:
    CREATE TABLE IF NOT EXISTS role_assignments (
        subject     TEXT NOT NULL,
        role        TEXT NOT NULL,
        assigned_by TEXT NOT NULL,
        assigned_at TEXT NOT NULL,
        PRIMARY KEY (subject, role)
    )
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

_DDL = """
CREATE TABLE IF NOT EXISTS role_assignments (
    subject     TEXT NOT NULL,
    role        TEXT NOT NULL,
    assigned_by TEXT NOT NULL,
    assigned_at TEXT NOT NULL,
    PRIMARY KEY (subject, role)
)
"""


class LastAdminError(Exception):
    """Raised when a revoke would leave the system with no admin path.

    See ADR-0060 for the invariant: at least one row with role='admin' in
    role_assignments must remain, OR an OIDC issuer must be configured via
    NOVA_OIDC_ISSUER so JWT-claim-based admins can still authenticate.
    """


def _get_conn(db_path: Path | None) -> sqlite3.Connection:
    from novafabric.registry.store import get_connection, get_db_path

    resolved = db_path or get_db_path()
    conn = get_connection(resolved)
    conn.row_factory = sqlite3.Row
    conn.execute(_DDL)
    conn.commit()
    return conn


def assign_role(
    subject: str,
    role: str,
    assigned_by: str,
    *,
    db_path: Path | None = None,
) -> None:
    """Insert or replace a role assignment for *subject*. Idempotent."""
    now = datetime.now(timezone.utc).isoformat()
    conn = _get_conn(db_path)
    try:
        conn.execute(
            """
            INSERT OR REPLACE INTO role_assignments
                (subject, role, assigned_by, assigned_at)
            VALUES (?, ?, ?, ?)
            """,
            (subject, role, assigned_by, now),
        )
        conn.commit()
    finally:
        conn.close()


def get_roles(subject: str, *, db_path: Path | None = None) -> list[str]:
    """Return the list of roles assigned to *subject* in the DB."""
    conn = _get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT role FROM role_assignments WHERE subject = ?", (subject,)
        ).fetchall()
        return [row["role"] for row in rows]
    finally:
        conn.close()


def list_assignments(*, db_path: Path | None = None) -> list[dict[str, str]]:
    """Return all role assignments."""
    conn = _get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT subject, role, assigned_by, assigned_at FROM role_assignments"
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def list_subjects(*, db_path: Path | None = None) -> list[str]:
    """Return the distinct subjects that have at least one role assignment."""
    conn = _get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT DISTINCT subject FROM role_assignments ORDER BY subject"
        ).fetchall()
        return [row["subject"] for row in rows]
    finally:
        conn.close()


def revoke_role(
    subject: str,
    role: str,
    *,
    db_path: Path | None = None,
) -> bool:
    """Revoke *role* from *subject*.

    Returns True if a row was deleted, False if the assignment did not exist.

    Raises LastAdminError if the deletion would leave the system with no admin
    path (no remaining ``role_assignments`` admin row AND no OIDC issuer
    configured via ``NOVA_OIDC_ISSUER``).
    """
    conn = _get_conn(db_path)
    try:
        exists = conn.execute(
            "SELECT COUNT(*) AS c FROM role_assignments WHERE subject = ? AND role = ?",
            (subject, role),
        ).fetchone()["c"]
        if exists == 0:
            # Deleting a row that does not exist can never cause admin lockout —
            # report not-found instead of tripping the last-admin guard below
            # (which previously fired on an empty store and masked the 404).
            return False
        if role == "admin" and not os.environ.get("NOVA_OIDC_ISSUER"):
            remaining = conn.execute(
                "SELECT COUNT(*) AS c FROM role_assignments "
                "WHERE role = 'admin' AND NOT (subject = ? AND role = ?)",
                (subject, role),
            ).fetchone()["c"]
            if remaining == 0:
                raise LastAdminError(
                    f"refusing to revoke last admin role from {subject!r}: "
                    "no other admin assignment remains and no OIDC issuer is configured"
                )
        cursor = conn.execute(
            "DELETE FROM role_assignments WHERE subject = ? AND role = ?",
            (subject, role),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()
