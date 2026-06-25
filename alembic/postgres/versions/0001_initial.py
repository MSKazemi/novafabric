"""Initial Postgres schema — equivalent to the SQLite schema with Postgres-native types.

Revision ID: p0001
Revises:
Create Date: 2026-05-10
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision: str = "p0001"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "schema_version",
        sa.Column("version", sa.Integer, primary_key=True),
    )
    op.execute("INSERT INTO schema_version VALUES (1) ON CONFLICT DO NOTHING")

    op.create_table(
        "assets",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("asset_type", sa.Text, nullable=False),
        sa.Column("version", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="development"),
        sa.Column("spec_json", sa.Text, nullable=False),
        sa.Column("git_commit_sha", sa.Text),
        sa.Column("created_at", sa.Text, nullable=False),
        sa.Column("promoted_at", sa.Text),
        sa.Column("promoted_by", sa.Text),
        sa.Column("forced_promotion", sa.Integer, nullable=False, server_default="0"),
        sa.UniqueConstraint("name", "version"),
    )

    op.create_table(
        "eval_results",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("asset_id", sa.Text, nullable=False),
        sa.Column("suite_name", sa.Text, nullable=False),
        sa.Column("passed", sa.Integer, nullable=False),
        sa.Column("score_json", sa.Text),
        sa.Column("run_at", sa.Text, nullable=False),
        sa.ForeignKeyConstraint(["asset_id"], ["assets.id"]),
    )

    op.create_table(
        "lineage_nodes",
        sa.Column("node_id", sa.Text, primary_key=True),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("ref", sa.Text, nullable=False),
        sa.Column("first_seen_capsule_run_id", sa.Text),
        sa.Column("payload", sa.Text, nullable=False),
        sa.UniqueConstraint("kind", "ref"),
    )

    op.create_table(
        "lineage_edges",
        sa.Column("edge_id", sa.Text, primary_key=True),
        sa.Column("edge_type", sa.Text, nullable=False),
        sa.Column("source_id", sa.Text, nullable=False),
        sa.Column("target_id", sa.Text, nullable=False),
        sa.Column("capsule_run_id", sa.Text, nullable=False),
        sa.Column("confidence", sa.Text),
        sa.Column("created_at", sa.Text, nullable=False),
        sa.Column("payload", sa.Text, nullable=False),
        sa.ForeignKeyConstraint(["source_id"], ["lineage_nodes.node_id"]),
        sa.ForeignKeyConstraint(["target_id"], ["lineage_nodes.node_id"]),
    )

    op.create_index("lineage_edges_source", "lineage_edges", ["source_id"])
    op.create_index("lineage_edges_target", "lineage_edges", ["target_id"])
    op.create_index("lineage_edges_capsule", "lineage_edges", ["capsule_run_id"])
    op.create_index("lineage_nodes_ref", "lineage_nodes", ["ref"])
    op.create_index("lineage_nodes_kind_ref", "lineage_nodes", ["kind", "ref"])


def downgrade() -> None:
    op.drop_index("lineage_nodes_kind_ref", table_name="lineage_nodes")
    op.drop_index("lineage_nodes_ref", table_name="lineage_nodes")
    op.drop_index("lineage_edges_capsule", table_name="lineage_edges")
    op.drop_index("lineage_edges_target", table_name="lineage_edges")
    op.drop_index("lineage_edges_source", table_name="lineage_edges")
    op.drop_table("lineage_edges")
    op.drop_table("lineage_nodes")
    op.drop_table("eval_results")
    op.drop_table("assets")
    op.drop_table("schema_version")
