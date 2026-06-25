"""Add approvals table and pending_approval status support (SQLite).

Revision ID: s0002
Revises: s0001
Create Date: 2026-05-10
"""
from __future__ import annotations

from alembic import op

revision: str = "s0002"
down_revision: str | None = "s0001"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # SQLite stores status as TEXT, so no ALTER needed for the status column —
    # TEXT already accepts the new "pending_approval" and "validated" values.
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS approvals (
            approval_id   TEXT PRIMARY KEY,
            asset_name    TEXT NOT NULL,
            asset_version TEXT NOT NULL,
            approver      TEXT NOT NULL,
            approved_at   TEXT NOT NULL,
            note          TEXT NOT NULL DEFAULT ''
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS approvals")
