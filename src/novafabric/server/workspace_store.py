"""Workspace / organization / service-account registry tier (ADR-0178, first slice).

Experimental. SQLite tables in the same registry DB the server already uses
(follows the ``rbac_store.py`` / ``scim_store.py`` pattern): ``organizations``,
``workspaces``, ``memberships``, ``service_accounts``.

NORMATIVE per spec I1/I2 (``the private design/spec/workspace-org-model-v0.md``): this module
makes NO ``tenant_id`` or RLS changes anywhere — this layer is scoping only.
``tenant_id`` remains the sole RLS isolation key; a workspace is never a security
boundary. Workspace access control is application-enforced on top of
database-enforced tenant isolation. The Postgres ``metadata_store`` / RLS layer is
explicitly OUT of this slice and is untouched.

Scoped role bindings (``memberships``) feed :func:`effective_roles`, which unions
them with the existing global ``role_assignments`` rows (ADR-0060,
``rbac_store.py``). Deployments that never create memberships or service accounts
are unaffected: :func:`feature_in_use` lets callers short-circuit to the
pre-ADR-0178 behavior.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ulid import ULID

from novafabric.server import rbac_store

#: The six-role vocabulary (ADR-0018 reader/writer/admin/auditor + ADR-0058
#: promoter/approver). Spec I5: no new roles; bindings only change where a role
#: attaches. Kept in sync with ``scim_group_mapping.VALID_SCIM_ROLES``.
VALID_ROLES: frozenset[str] = frozenset(
    {"reader", "writer", "admin", "auditor", "promoter", "approver"}
)

#: Prefix for service-account token subjects (offline ed25519 JWT ``sub`` claim).
SERVICE_ACCOUNT_SUBJECT_PREFIX = "svc:"

_DDL = """
CREATE TABLE IF NOT EXISTS organizations (
    id         TEXT PRIMARY KEY,
    slug       TEXT NOT NULL UNIQUE,
    name       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS workspaces (
    id         TEXT PRIMARY KEY,
    org_id     TEXT NOT NULL REFERENCES organizations(id),
    slug       TEXT NOT NULL,
    name       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    UNIQUE (org_id, slug)
);
CREATE TABLE IF NOT EXISTS memberships (
    principal  TEXT NOT NULL,
    scope_type TEXT NOT NULL CHECK (scope_type IN ('org', 'workspace')),
    scope_id   TEXT NOT NULL,
    role       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    UNIQUE (principal, scope_type, scope_id, role)
);
CREATE TABLE IF NOT EXISTS service_accounts (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    created_by  TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    disabled    INTEGER NOT NULL DEFAULT 0,
    token_jti   TEXT
);
"""

DEFAULT_ORG_SLUG = "default"
DEFAULT_WORKSPACE_SLUG = "default"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SlugConflictError(Exception):
    """A slug (or service-account name) collides with an existing row."""


class ScopeNotEmptyError(Exception):
    """Deletion refused: the org/workspace still contains dependent rows."""


class UnknownScopeError(KeyError):
    """The referenced org, workspace, or service account does not exist."""


class InvalidRoleError(ValueError):
    """A membership names a role outside the six-role vocabulary (spec I5)."""


# ---------------------------------------------------------------------------
# Connection helper
# ---------------------------------------------------------------------------


def _get_conn(db_path: Path | None) -> sqlite3.Connection:
    from novafabric.registry.store import get_connection, get_db_path

    resolved = db_path or get_db_path()
    conn = get_connection(resolved)
    conn.row_factory = sqlite3.Row
    conn.executescript(_DDL)
    conn.commit()
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return str(ULID())


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------


def ensure_default(
    *, created_by: str = "system", db_path: Path | None = None
) -> tuple[str, str]:
    """Idempotently create the ``default`` org and ``default`` workspace.

    Implements the spec's migration rule: existing single-team deployments land
    in a default hierarchy with no operator action. Returns
    ``(org_id, workspace_id)``; repeated calls return the same ids.
    """
    conn = _get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT id FROM organizations WHERE slug = ?", (DEFAULT_ORG_SLUG,)
        ).fetchone()
        if row is None:
            org_id = _new_id()
            conn.execute(
                "INSERT INTO organizations (id, slug, name, created_at, created_by)"
                " VALUES (?, ?, ?, ?, ?)",
                (org_id, DEFAULT_ORG_SLUG, "Default organization", _now(), created_by),
            )
        else:
            org_id = row["id"]
        row = conn.execute(
            "SELECT id FROM workspaces WHERE org_id = ? AND slug = ?",
            (org_id, DEFAULT_WORKSPACE_SLUG),
        ).fetchone()
        if row is None:
            ws_id = _new_id()
            conn.execute(
                "INSERT INTO workspaces (id, org_id, slug, name, created_at, created_by)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (ws_id, org_id, DEFAULT_WORKSPACE_SLUG, "Default workspace", _now(), created_by),
            )
        else:
            ws_id = row["id"]
        conn.commit()
        return org_id, ws_id
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Organizations
# ---------------------------------------------------------------------------


def create_org(
    slug: str, name: str, created_by: str, *, db_path: Path | None = None
) -> dict[str, Any]:
    """Create an organization. Raises :class:`SlugConflictError` on a dup slug."""
    conn = _get_conn(db_path)
    try:
        org_id = _new_id()
        try:
            conn.execute(
                "INSERT INTO organizations (id, slug, name, created_at, created_by)"
                " VALUES (?, ?, ?, ?, ?)",
                (org_id, slug, name, _now(), created_by),
            )
        except sqlite3.IntegrityError as exc:
            raise SlugConflictError(f"organization slug {slug!r} already exists") from exc
        conn.commit()
        row = conn.execute("SELECT * FROM organizations WHERE id = ?", (org_id,)).fetchone()
        return dict(row)
    finally:
        conn.close()


def list_orgs(*, db_path: Path | None = None) -> list[dict[str, Any]]:
    conn = _get_conn(db_path)
    try:
        rows = conn.execute("SELECT * FROM organizations ORDER BY slug").fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_org(org_id: str, *, db_path: Path | None = None) -> dict[str, Any] | None:
    conn = _get_conn(db_path)
    try:
        row = conn.execute("SELECT * FROM organizations WHERE id = ?", (org_id,)).fetchone()
        return dict(row) if row is not None else None
    finally:
        conn.close()


def delete_org(org_id: str, *, db_path: Path | None = None) -> None:
    """Delete an org. Only allowed when it has no workspaces and no memberships.

    Raises :class:`UnknownScopeError` if missing, :class:`ScopeNotEmptyError`
    if the org still contains workspaces or org-scoped memberships.
    """
    conn = _get_conn(db_path)
    try:
        if conn.execute(
            "SELECT 1 FROM organizations WHERE id = ?", (org_id,)
        ).fetchone() is None:
            raise UnknownScopeError(f"organization {org_id!r} not found")
        n_ws = conn.execute(
            "SELECT COUNT(*) AS c FROM workspaces WHERE org_id = ?", (org_id,)
        ).fetchone()["c"]
        n_mem = conn.execute(
            "SELECT COUNT(*) AS c FROM memberships WHERE scope_type = 'org' AND scope_id = ?",
            (org_id,),
        ).fetchone()["c"]
        if n_ws or n_mem:
            raise ScopeNotEmptyError(
                f"organization {org_id!r} is not empty "
                f"({n_ws} workspace(s), {n_mem} membership(s))"
            )
        conn.execute("DELETE FROM organizations WHERE id = ?", (org_id,))
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Workspaces
# ---------------------------------------------------------------------------


def create_workspace(
    org_id: str, slug: str, name: str, created_by: str, *, db_path: Path | None = None
) -> dict[str, Any]:
    """Create a workspace in *org_id*. Slug must be unique within the org."""
    conn = _get_conn(db_path)
    try:
        if conn.execute(
            "SELECT 1 FROM organizations WHERE id = ?", (org_id,)
        ).fetchone() is None:
            raise UnknownScopeError(f"organization {org_id!r} not found")
        ws_id = _new_id()
        try:
            conn.execute(
                "INSERT INTO workspaces (id, org_id, slug, name, created_at, created_by)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (ws_id, org_id, slug, name, _now(), created_by),
            )
        except sqlite3.IntegrityError as exc:
            raise SlugConflictError(
                f"workspace slug {slug!r} already exists in organization {org_id!r}"
            ) from exc
        conn.commit()
        row = conn.execute("SELECT * FROM workspaces WHERE id = ?", (ws_id,)).fetchone()
        return dict(row)
    finally:
        conn.close()


def list_workspaces(
    *, org_id: str | None = None, db_path: Path | None = None
) -> list[dict[str, Any]]:
    conn = _get_conn(db_path)
    try:
        if org_id is None:
            rows = conn.execute("SELECT * FROM workspaces ORDER BY org_id, slug").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM workspaces WHERE org_id = ? ORDER BY slug", (org_id,)
            ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_workspace(ws_id: str, *, db_path: Path | None = None) -> dict[str, Any] | None:
    conn = _get_conn(db_path)
    try:
        row = conn.execute("SELECT * FROM workspaces WHERE id = ?", (ws_id,)).fetchone()
        return dict(row) if row is not None else None
    finally:
        conn.close()


def delete_workspace(ws_id: str, *, db_path: Path | None = None) -> None:
    """Delete a workspace. Only allowed when it has no memberships."""
    conn = _get_conn(db_path)
    try:
        if conn.execute("SELECT 1 FROM workspaces WHERE id = ?", (ws_id,)).fetchone() is None:
            raise UnknownScopeError(f"workspace {ws_id!r} not found")
        n_mem = conn.execute(
            "SELECT COUNT(*) AS c FROM memberships"
            " WHERE scope_type = 'workspace' AND scope_id = ?",
            (ws_id,),
        ).fetchone()["c"]
        if n_mem:
            raise ScopeNotEmptyError(
                f"workspace {ws_id!r} is not empty ({n_mem} membership(s))"
            )
        conn.execute("DELETE FROM workspaces WHERE id = ?", (ws_id,))
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Memberships (scoped role bindings)
# ---------------------------------------------------------------------------


def add_membership(
    principal: str,
    scope_type: str,
    scope_id: str,
    role: str,
    created_by: str,
    *,
    db_path: Path | None = None,
) -> bool:
    """Grant a scoped role binding. Idempotent; returns True if a row was added.

    Validates the role against the six-role vocabulary (spec I5) and that the
    scope exists. ``scope_type`` is ``org`` or ``workspace``.
    """
    if role not in VALID_ROLES:
        raise InvalidRoleError(
            f"role {role!r} is not one of the six roles: {sorted(VALID_ROLES)}"
        )
    if scope_type not in ("org", "workspace"):
        raise InvalidRoleError(f"scope_type must be 'org' or 'workspace', got {scope_type!r}")
    conn = _get_conn(db_path)
    try:
        table = "organizations" if scope_type == "org" else "workspaces"
        if conn.execute(
            f"SELECT 1 FROM {table} WHERE id = ?", (scope_id,)  # noqa: S608
        ).fetchone() is None:
            raise UnknownScopeError(f"{scope_type} {scope_id!r} not found")
        cur = conn.execute(
            "INSERT OR IGNORE INTO memberships"
            " (principal, scope_type, scope_id, role, created_at, created_by)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (principal, scope_type, scope_id, role, _now(), created_by),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def remove_membership(
    principal: str,
    scope_type: str,
    scope_id: str,
    role: str,
    *,
    db_path: Path | None = None,
) -> bool:
    """Revoke a scoped role binding. Returns True if a row was deleted."""
    conn = _get_conn(db_path)
    try:
        cur = conn.execute(
            "DELETE FROM memberships WHERE principal = ? AND scope_type = ?"
            " AND scope_id = ? AND role = ?",
            (principal, scope_type, scope_id, role),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def list_memberships(
    *,
    scope_type: str | None = None,
    scope_id: str | None = None,
    principal: str | None = None,
    db_path: Path | None = None,
) -> list[dict[str, Any]]:
    conn = _get_conn(db_path)
    try:
        clauses: list[str] = []
        params: list[str] = []
        if scope_type is not None:
            clauses.append("scope_type = ?")
            params.append(scope_type)
        if scope_id is not None:
            clauses.append("scope_id = ?")
            params.append(scope_id)
        if principal is not None:
            clauses.append("principal = ?")
            params.append(principal)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = conn.execute(
            f"SELECT * FROM memberships{where} ORDER BY principal, role",  # noqa: S608
            params,
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Service accounts
# ---------------------------------------------------------------------------


def create_service_account(
    name: str,
    description: str,
    created_by: str,
    *,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """Create a service account. Raises :class:`SlugConflictError` on a dup name."""
    conn = _get_conn(db_path)
    try:
        account_id = _new_id()
        try:
            conn.execute(
                "INSERT INTO service_accounts"
                " (id, name, description, created_by, created_at, disabled, token_jti)"
                " VALUES (?, ?, ?, ?, ?, 0, NULL)",
                (account_id, name, description, created_by, _now()),
            )
        except sqlite3.IntegrityError as exc:
            raise SlugConflictError(f"service account {name!r} already exists") from exc
        conn.commit()
        row = conn.execute(
            "SELECT * FROM service_accounts WHERE id = ?", (account_id,)
        ).fetchone()
        return dict(row)
    finally:
        conn.close()


def list_service_accounts(*, db_path: Path | None = None) -> list[dict[str, Any]]:
    conn = _get_conn(db_path)
    try:
        rows = conn.execute("SELECT * FROM service_accounts ORDER BY name").fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_service_account(
    account_id: str, *, db_path: Path | None = None
) -> dict[str, Any] | None:
    conn = _get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM service_accounts WHERE id = ?", (account_id,)
        ).fetchone()
        return dict(row) if row is not None else None
    finally:
        conn.close()


def get_service_account_by_name(
    name: str, *, db_path: Path | None = None
) -> dict[str, Any] | None:
    conn = _get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM service_accounts WHERE name = ?", (name,)
        ).fetchone()
        return dict(row) if row is not None else None
    finally:
        conn.close()


def set_service_account_token(
    account_id: str, token_jti: str, *, db_path: Path | None = None
) -> None:
    """Record the jti of the offline token minted for *account_id*."""
    conn = _get_conn(db_path)
    try:
        cur = conn.execute(
            "UPDATE service_accounts SET token_jti = ? WHERE id = ?",
            (token_jti, account_id),
        )
        conn.commit()
        if cur.rowcount == 0:
            raise UnknownScopeError(f"service account {account_id!r} not found")
    finally:
        conn.close()


def disable_service_account(
    account_id: str, *, db_path: Path | None = None
) -> dict[str, Any]:
    """Mark *account_id* disabled (idempotent). Returns the updated row.

    Token revocation (spec I4) is the caller's job via the existing
    ``offline_tokens.revoke_token`` path; the disabled flag is additionally
    checked at verification time as a second layer.
    """
    conn = _get_conn(db_path)
    try:
        cur = conn.execute(
            "UPDATE service_accounts SET disabled = 1 WHERE id = ?", (account_id,)
        )
        conn.commit()
        if cur.rowcount == 0:
            raise UnknownScopeError(f"service account {account_id!r} not found")
        row = conn.execute(
            "SELECT * FROM service_accounts WHERE id = ?", (account_id,)
        ).fetchone()
        return dict(row)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Effective-role resolution (spec I3)
# ---------------------------------------------------------------------------


def feature_in_use(*, db_path: Path | None = None) -> bool:
    """True when any scoped membership or service account exists.

    Lets ``rbac.require_role`` short-circuit to the exact pre-ADR-0178 path for
    deployments that never use the workspace/org model.
    """
    conn = _get_conn(db_path)
    try:
        n = conn.execute(
            "SELECT (SELECT COUNT(*) FROM memberships)"
            " + (SELECT COUNT(*) FROM service_accounts) AS c"
        ).fetchone()["c"]
        return bool(n)
    finally:
        conn.close()


def effective_roles(
    principal: str,
    workspace_id: str | None = None,
    *,
    db_path: Path | None = None,
) -> set[str]:
    """Union of the principal's global, org-scoped, and workspace-scoped roles.

    Spec I3: effective role = max over all applicable bindings; because the
    caller checks the union against the existing role hierarchy
    (``rbac._satisfies``), max-over-bindings and orthogonal-role union both
    come free from returning the plain union here.

    - ``workspace_id is None`` — union across ALL of the principal's scoped
      bindings plus global ``role_assignments`` rows (used by the request-level
      fallback, which has no workspace context in this slice).
    - ``workspace_id`` given — global rows, org bindings on the containing org,
      and workspace bindings on that workspace only.
    """
    roles: set[str] = set(rbac_store.get_roles(principal, db_path=db_path))
    conn = _get_conn(db_path)
    try:
        if workspace_id is None:
            rows = conn.execute(
                "SELECT role FROM memberships WHERE principal = ?", (principal,)
            ).fetchall()
        else:
            ws = conn.execute(
                "SELECT org_id FROM workspaces WHERE id = ?", (workspace_id,)
            ).fetchone()
            org_id = ws["org_id"] if ws is not None else None
            rows = conn.execute(
                "SELECT role FROM memberships WHERE principal = ? AND ("
                " (scope_type = 'workspace' AND scope_id = ?)"
                " OR (scope_type = 'org' AND scope_id = ?))",
                (principal, workspace_id, org_id),
            ).fetchall()
        roles.update(row["role"] for row in rows)
        return roles
    finally:
        conn.close()
