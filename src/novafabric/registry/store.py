from __future__ import annotations

import sqlite3
from pathlib import Path

from novafabric._paths import registry_db_path


def get_db_path() -> Path:
    return registry_db_path()


def get_connection(db_path: Path | None = None) -> sqlite3.Connection:
    path = db_path or get_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.commit()
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER PRIMARY KEY
        );
        INSERT OR IGNORE INTO schema_version VALUES (1);

        CREATE TABLE IF NOT EXISTS assets (
            id          TEXT PRIMARY KEY,
            name        TEXT NOT NULL,
            asset_type  TEXT NOT NULL,
            version     TEXT NOT NULL,
            status      TEXT NOT NULL DEFAULT 'development',
            spec_json   TEXT NOT NULL,
            git_commit_sha TEXT,
            created_at  TEXT NOT NULL,
            promoted_at TEXT,
            promoted_by TEXT,
            forced_promotion INTEGER NOT NULL DEFAULT 0,
            UNIQUE(name, version)
        );

        CREATE TABLE IF NOT EXISTS eval_results (
            id          TEXT PRIMARY KEY,
            asset_id    TEXT NOT NULL,
            suite_name  TEXT NOT NULL,
            passed      INTEGER NOT NULL,
            score_json  TEXT,
            run_at      TEXT NOT NULL,
            FOREIGN KEY(asset_id) REFERENCES assets(id)
        );

        CREATE TABLE IF NOT EXISTS approvals (
            approval_id   TEXT PRIMARY KEY,
            asset_name    TEXT NOT NULL,
            asset_version TEXT NOT NULL,
            approver      TEXT NOT NULL,
            approved_at   TEXT NOT NULL,
            note          TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS promotion_proposals (
            proposal_id      TEXT PRIMARY KEY,
            asset_name       TEXT NOT NULL,
            asset_version    TEXT NOT NULL,
            to_status        TEXT NOT NULL,
            proposer         TEXT NOT NULL,
            proposer_key_fp  TEXT NOT NULL,
            proposer_sig     TEXT NOT NULL,
            proposed_at      TEXT NOT NULL,
            state            TEXT NOT NULL DEFAULT 'open',
            approver         TEXT,
            approver_key_fp  TEXT,
            approver_sig     TEXT,
            approved_at      TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_assets_created_at ON assets(created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_assets_status ON assets(status);
        CREATE INDEX IF NOT EXISTS idx_assets_asset_type ON assets(asset_type);
        CREATE INDEX IF NOT EXISTS idx_eval_results_asset_id ON eval_results(asset_id);
        CREATE INDEX IF NOT EXISTS idx_eval_results_run_at ON eval_results(run_at DESC);
        """
    )
    # Scale-S1: runs_cache — indexed capsule summaries (replaces O(N) disk scan)
    from novafabric.registry.runs_cache import ensure_runs_cache  # noqa: PLC0415
    ensure_runs_cache(conn)
