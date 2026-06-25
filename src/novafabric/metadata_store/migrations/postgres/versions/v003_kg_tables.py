"""KG alias table + entity resolution queue DDL.

Adds two tables required by the Tier-2/Tier-3 entity resolution pipeline
(ADR-0067 Capsule Knowledge Graph v1).

Tables are NOT tenant-scoped (they hold organisation-wide canonical name
mappings and review items, not per-tenant data), so no RLS policies are
applied here.  Access should be controlled via the ``novafabric_app`` role
permissions granted in v001.

Revision ID: v003
Revises: v002
Create Date: 2026-05-19

References
----------
- ADR-0067: Capsule Knowledge Graph v1
- src/novafabric/kg/alias_resolver.py — Tier-2 resolver (reads kg_alias_table)
- src/novafabric/kg/review_queue.py   — Tier-3 queue writer (writes kg_entity_resolution_queue)
"""
from __future__ import annotations

revision = "v003"
down_revision = "v002"
branch_labels = None
depends_on = None

# ---------------------------------------------------------------------------
# DDL strings — applied by run_migrations() / nova kg init --postgres
# ---------------------------------------------------------------------------

UPGRADE_SQL = """
-- Tier-2: alias table maps raw entity names to canonical forms
CREATE TABLE IF NOT EXISTS kg_alias_table (
    raw_entity        TEXT        NOT NULL,
    canonical_entity  TEXT        NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT kg_alias_table_pkey PRIMARY KEY (raw_entity)
);

-- Tier-3: entity resolution queue holds entities needing human review
CREATE TABLE IF NOT EXISTS kg_entity_resolution_queue (
    id             BIGSERIAL   PRIMARY KEY,
    raw_entity     TEXT        NOT NULL,
    context_run_id TEXT,
    confidence     FLOAT       NOT NULL,
    status         TEXT        NOT NULL DEFAULT 'pending',
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS kg_erq_status_idx
    ON kg_entity_resolution_queue(status);
CREATE INDEX IF NOT EXISTS kg_erq_raw_entity_idx
    ON kg_entity_resolution_queue(raw_entity);
"""

DOWNGRADE_SQL = """
DROP TABLE IF EXISTS kg_entity_resolution_queue;
DROP TABLE IF EXISTS kg_alias_table;
"""


def upgrade(conn: object) -> None:
    """Apply KG tables DDL.  *conn* must support ``conn.execute(sql)``."""
    for statement in UPGRADE_SQL.strip().split(";"):
        stmt = statement.strip()
        if stmt:
            conn.execute(stmt)  # type: ignore[attr-defined]


def downgrade(conn: object) -> None:
    """Remove KG tables.  Destructive — use with care."""
    for statement in DOWNGRADE_SQL.strip().split(";"):
        stmt = statement.strip()
        if stmt:
            conn.execute(stmt)  # type: ignore[attr-defined]
