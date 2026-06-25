"""Canonical RLS policy helpers for the NovaFabric MetadataStore.

All SET LOCAL / current_setting(...) / FORCE ROW LEVEL SECURITY text is confined
to this package (FR-13). The CI grep gate enforces this constraint.

TODO: find source — empirical pgBouncer + RLS community guidance recommending
SET LOCAL over session-scoped SET (Crunchy Data / Supabase engineering /
pgsql-hackers post). Required before ADR-001 / ADR-003 promote to 'accepted'.
"""
from __future__ import annotations

from typing import Any

import psycopg

TENANT_SCOPED_TABLES = ("runs", "capsules", "signatures", "retention_policies")

# FR-10: canonical policy qual — both USING and WITH CHECK must match this form.
# Verified against Postgres 16-alpine via pg_get_expr() on a live testcontainer:
#
#   SELECT pg_get_expr(polqual, polrelid)
#   FROM pg_policy WHERE polname = 'tenant_isolation' LIMIT 1
#
# Returns: "(tenant_id = (current_setting('app.current_tenant_id'::text))::uuid)"
#
# Note: pg_get_expr() normalises the expression; the ::text cast on the GUC key
# and the surrounding parentheses are added by Postgres during deparse.
CANONICAL_POLICY_QUAL = "(tenant_id = (current_setting('app.current_tenant_id'::text))::uuid)"


def verify_force_rls(conn: psycopg.Connection[Any], tables: tuple[str, ...]) -> dict[str, bool]:
    """Return {table_name: relforcerowsecurity_bool} for the given tables."""
    rows = conn.execute(
        "SELECT relname, relforcerowsecurity FROM pg_class WHERE relname = ANY(%s)",
        (list(tables),),
    ).fetchall()
    return {row[0]: bool(row[1]) for row in rows}


def verify_policy_text(conn: psycopg.Connection[Any], tables: tuple[str, ...]) -> dict[str, bool]:
    """Return {table_name: bool} — True if pg_policy.polqual matches canonical form."""
    rows = conn.execute(
        """SELECT c.relname, pg_get_expr(p.polqual, p.polrelid) AS qual
           FROM pg_policy p
           JOIN pg_class c ON c.oid = p.polrelid
           WHERE c.relname = ANY(%s) AND p.polname = 'tenant_isolation'""",
        (list(tables),),
    ).fetchall()
    return {row[0]: row[1] == CANONICAL_POLICY_QUAL for row in rows}


def verify_role_split(conn: psycopg.Connection[Any]) -> dict[str, Any]:
    """Return role audit dict with novafabric_app_bypassrls and novafabric_migrator_bypassrls."""
    rows = conn.execute(
        "SELECT rolname, rolbypassrls FROM pg_roles"
        " WHERE rolname IN ('novafabric_app', 'novafabric_migrator')"
    ).fetchall()
    result: dict[str, Any] = {}
    for row in rows:
        if row[0] == "novafabric_app":
            result["novafabric_app_bypassrls"] = bool(row[1])
        elif row[0] == "novafabric_migrator":
            result["novafabric_migrator_bypassrls"] = bool(row[1])
    return result
