from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from novafabric._paths import registry_db_path
from novafabric._sqlite_util import ensure_wal

# Databases whose schema this process has already ensured. The DDL below is
# all idempotent CREATE IF NOT EXISTS, but parsing + executing the script on
# EVERY request was measurable overhead on the dashboard's hot read paths —
# once per (process, db file) is enough. Keyed by the resolved main-db path so
# tests with per-test tmp DBs each still get their DDL.
_SCHEMA_READY: set[str] = set()
_SCHEMA_READY_LOCK = threading.Lock()


def reset_schema_memo() -> None:
    """Forget which databases were initialised (tests / DB replacement)."""
    with _SCHEMA_READY_LOCK:
        _SCHEMA_READY.clear()


def get_db_path() -> Path:
    return registry_db_path()


def get_connection(db_path: Path | None = None) -> sqlite3.Connection:
    path = db_path or get_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    ensure_wal(conn, what="registry database")
    return conn


def _main_db_file(conn: sqlite3.Connection) -> str | None:
    try:
        for _, name, file in conn.execute("PRAGMA database_list"):
            if name == "main":
                return file or None
    except sqlite3.Error:
        return None
    return None


def init_schema(conn: sqlite3.Connection, *, force: bool = False) -> None:
    """Ensure the registry schema exists, running the DDL once per (process, db file).

    The memo is checked and set under **one** hold of the lock, with the DDL inside it.
    This used to be a check-then-act: the membership test took the lock, released it, ran
    ``_init_schema`` unlocked, then took the lock again to record the result. Two threads
    could therefore both miss the memo and both run the full ``executescript`` — which is
    precisely the repeated-DDL cost this memo exists to remove, so under concurrency the
    optimisation quietly stopped applying.

    The window is not theoretical: ``serve`` calls this from four sites that run
    concurrently against the same db file — the lifespan's ``run_in_executor`` bootstrap,
    the stats refresh thread, the capsule-watcher thread, and the request handlers.
    ``tests/serve/test_schema_init_once.py`` asserts once-per-db and had been passing only
    because the window is narrow; it failed with ``2 == 1`` once the per-app stats cache
    (ADR-0257) began cold-starting its refresh thread alongside the first request.

    Holding the lock across the DDL serialises first-init across *all* db files rather than
    per file. That is deliberate: the work is idempotent ``CREATE IF NOT EXISTS``, it runs
    once per file per process, and production has exactly one registry db — a per-file lock
    table would buy nothing for the extra state. No deadlock is possible, because a thread
    waiting on this lock holds no sqlite lock: it has not begun the DDL yet.
    """
    dbfile = _main_db_file(conn)
    if dbfile is None:
        # Unknowable path (in-memory db, or PRAGMA database_list failed): cannot memo.
        _init_schema(conn)
        return
    with _SCHEMA_READY_LOCK:
        if dbfile in _SCHEMA_READY and not force:
            return
        _init_schema(conn)
        _SCHEMA_READY.add(dbfile)


def _init_schema(conn: sqlite3.Connection) -> None:
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

        -- ADR-0121 P3: comments on registry assets. Capsule comments live in
        -- the capsule's append-only comments.jsonl; assets have no capsule, so
        -- they get a table here. APPEND-ONLY BY CONVENTION, exactly like the
        -- JSONL side: rows are never UPDATEd or DELETEd — an edit is a reply
        -- (`in_reply_to`) and a delete is a tombstone row. That keeps the two
        -- storage backends semantically identical, so `Comment` records round
        -- trip through either without special-casing.
        CREATE TABLE IF NOT EXISTS asset_comments (
            comment_id        TEXT PRIMARY KEY,
            subject           TEXT NOT NULL,   -- asset://<type>/<name>@<version>
            subject_kind      TEXT NOT NULL DEFAULT 'asset',
            author            TEXT NOT NULL,
            created_at        TEXT NOT NULL,
            body              TEXT NOT NULL,
            in_reply_to       TEXT,
            tombstone         INTEGER NOT NULL DEFAULT 0,
            redaction_applied INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_asset_comments_subject
            ON asset_comments(subject, created_at);

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
