"""Initial SQLite schema for MetadataStore (dev-only).

Revision ID: v001
Revises:
Create Date: 2026-05-13

NOTE: bootstrap() in SQLiteMetadataStore creates these tables inline via
executescript().  This migration file exists so that ``alembic upgrade head``
produces an identical schema for fresh dev environments.
"""
from __future__ import annotations

from alembic import op

revision = "v001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS runs (
            run_id            TEXT NOT NULL,
            tenant_id         TEXT NOT NULL,
            event_type        TEXT,
            global_run_id     TEXT,
            started_at        TEXT,
            status            TEXT NOT NULL DEFAULT 'pending',
            world_size        INTEGER,
            expected_children INTEGER,
            children_arrived  INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (run_id, tenant_id)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS capsules (
            capsule_uri TEXT NOT NULL PRIMARY KEY,
            run_id      TEXT NOT NULL,
            tenant_id   TEXT NOT NULL
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS signatures (
            run_id         TEXT NOT NULL,
            tenant_id      TEXT NOT NULL,
            signature_hash TEXT NOT NULL,
            payload_json   TEXT NOT NULL,
            PRIMARY KEY (run_id, signature_hash)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS retention_policies (
            tenant_id   TEXT NOT NULL PRIMARY KEY,
            policy_json TEXT NOT NULL
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS retention_policies")
    op.execute("DROP TABLE IF EXISTS signatures")
    op.execute("DROP TABLE IF EXISTS capsules")
    op.execute("DROP TABLE IF EXISTS runs")
