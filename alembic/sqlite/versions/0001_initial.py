"""Initial SQLite schema — mirrors the in-code DDL from registry/store.py and lineage/_store.py.

Revision ID: s0001
Revises:
Create Date: 2026-05-10
"""
from __future__ import annotations

from alembic import op

revision: str = "s0001"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER PRIMARY KEY
        )
        """
    )
    op.execute("INSERT OR IGNORE INTO schema_version VALUES (1)")

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS assets (
            id              TEXT PRIMARY KEY,
            name            TEXT NOT NULL,
            asset_type      TEXT NOT NULL,
            version         TEXT NOT NULL,
            status          TEXT NOT NULL DEFAULT 'development',
            spec_json       TEXT NOT NULL,
            git_commit_sha  TEXT,
            created_at      TEXT NOT NULL,
            promoted_at     TEXT,
            promoted_by     TEXT,
            forced_promotion INTEGER NOT NULL DEFAULT 0,
            UNIQUE(name, version)
        )
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS eval_results (
            id          TEXT PRIMARY KEY,
            asset_id    TEXT NOT NULL,
            suite_name  TEXT NOT NULL,
            passed      INTEGER NOT NULL,
            score_json  TEXT,
            run_at      TEXT NOT NULL,
            FOREIGN KEY(asset_id) REFERENCES assets(id)
        )
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS lineage_nodes (
            node_id                   TEXT PRIMARY KEY,
            kind                      TEXT NOT NULL,
            ref                       TEXT NOT NULL,
            first_seen_capsule_run_id TEXT,
            payload                   TEXT NOT NULL,
            UNIQUE(kind, ref)
        )
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS lineage_edges (
            edge_id        TEXT PRIMARY KEY,
            edge_type      TEXT NOT NULL,
            source_id      TEXT NOT NULL REFERENCES lineage_nodes(node_id),
            target_id      TEXT NOT NULL REFERENCES lineage_nodes(node_id),
            capsule_run_id TEXT NOT NULL,
            confidence     TEXT,
            created_at     TEXT NOT NULL,
            payload        TEXT NOT NULL
        )
        """
    )

    op.execute(
        "CREATE INDEX IF NOT EXISTS lineage_edges_source ON lineage_edges(source_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS lineage_edges_target ON lineage_edges(target_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS lineage_edges_capsule ON lineage_edges(capsule_run_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS lineage_nodes_ref ON lineage_nodes(ref)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS lineage_nodes_kind_ref ON lineage_nodes(kind, ref)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS lineage_nodes_kind_ref")
    op.execute("DROP INDEX IF EXISTS lineage_nodes_ref")
    op.execute("DROP INDEX IF EXISTS lineage_edges_capsule")
    op.execute("DROP INDEX IF EXISTS lineage_edges_target")
    op.execute("DROP INDEX IF EXISTS lineage_edges_source")
    op.execute("DROP TABLE IF EXISTS lineage_edges")
    op.execute("DROP TABLE IF EXISTS lineage_nodes")
    op.execute("DROP TABLE IF EXISTS eval_results")
    op.execute("DROP TABLE IF EXISTS assets")
    op.execute("DROP TABLE IF EXISTS schema_version")
