"""Add approvals table and pending_approval status support (Postgres).

Revision ID: p0002
Revises: p0001
Create Date: 2026-05-10
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "p0002"
down_revision: str | None = "p0001"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # status is stored as TEXT/VARCHAR in Postgres (see p0001), so no ALTER
    # needed for the column — TEXT already accepts the new "pending_approval"
    # and "validated" values.
    op.create_table(
        "approvals",
        sa.Column("approval_id", sa.Text, primary_key=True),
        sa.Column("asset_name", sa.Text, nullable=False),
        sa.Column("asset_version", sa.Text, nullable=False),
        sa.Column("approver", sa.Text, nullable=False),
        sa.Column("approved_at", sa.Text, nullable=False),
        sa.Column("note", sa.Text, nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_table("approvals")
