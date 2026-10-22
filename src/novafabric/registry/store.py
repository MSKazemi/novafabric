from __future__ import annotations

import hashlib
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
            approved_at      TEXT,
            signoff_scheme   TEXT  -- ADR-0312; NULL = legacy, unverified
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
    _ensure_status_history(conn)
    _ensure_additive_columns(conn)
    # Scale-S1: runs_cache — indexed capsule summaries (replaces O(N) disk scan)
    from novafabric.registry.runs_cache import ensure_runs_cache  # noqa: PLC0415
    ensure_runs_cache(conn)


# Additive, nullable columns added after a table first shipped: (table, column, type).
# A database created before the column existed gets it via ALTER TABLE ADD COLUMN;
# existing rows read NULL. Never drop or retype a column here.
_ADDITIVE_COLUMNS: tuple[tuple[str, str, str], ...] = (
    # ADR-0312: signature scheme a proposal was verified under. NULL = stored before
    # signatures were verified (reported as "unverified").
    ("promotion_proposals", "signoff_scheme", "TEXT"),
    # Rollback evidence is bound to the registration it was written for, not to a
    # reusable name@version: the asset row id and the SHA-256 of its spec_json.
    # NULL = written before the binding existed (see rollback's legacy rule).
    ("asset_status_history", "asset_id", "TEXT"),
    ("asset_status_history", "spec_sha256", "TEXT"),
)


#: Append-only lifecycle history (additive table). ``nova rollback`` restores a
#: version without the ADR-0312 ``promote_direct`` gate only when this table shows
#: it was in ``production`` before; any other target is gated like a direct promote.
_STATUS_HISTORY_SQL = """
CREATE TABLE IF NOT EXISTS asset_status_history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_name   TEXT NOT NULL,
    version      TEXT NOT NULL,
    from_status  TEXT,
    to_status    TEXT NOT NULL,
    changed_at   TEXT NOT NULL,
    changed_by   TEXT NOT NULL,
    via          TEXT NOT NULL,
    asset_id     TEXT,
    spec_sha256  TEXT
);
CREATE INDEX IF NOT EXISTS idx_asset_status_history_name
    ON asset_status_history(asset_name, to_status, changed_at DESC);
CREATE TRIGGER IF NOT EXISTS trg_asset_status_history_no_update
    BEFORE UPDATE ON asset_status_history
    BEGIN SELECT RAISE(ABORT, 'asset_status_history is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_asset_status_history_no_delete
    BEFORE DELETE ON asset_status_history
    BEGIN SELECT RAISE(ABORT, 'asset_status_history is append-only'); END;
"""


def _ensure_status_history(conn: sqlite3.Connection) -> None:
    """Create ``asset_status_history``; on first creation, backfill current production.

    A registry that predates the table has no record of which versions were in
    production. The versions in ``production`` right now are the one fact it can
    still state, so they are recorded with ``via='backfill'``. That keeps the usual
    upgrade path working: today's production version can be rolled back to after a
    later release replaces it. A version archived *before* the upgrade has no
    evidence and goes through the ``promote_direct`` gate.
    """
    existed = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'asset_status_history'"
    ).fetchone()
    conn.executescript(_STATUS_HISTORY_SQL)
    if not existed:
        # Positional access: init_schema may be handed a connection without Row factory.
        for asset_id, name, version, spec_json, at, by in conn.execute(
            "SELECT id, name, version, spec_json, COALESCE(promoted_at, created_at), "
            "COALESCE(promoted_by, '') FROM assets WHERE status = 'production'"
        ).fetchall():
            record_status_change(
                conn,
                asset_name=name,
                version=version,
                from_status=None,
                to_status="production",
                changed_at=at,
                changed_by=by,
                via="backfill",
                asset_id=asset_id,
                spec_sha256=spec_digest(spec_json),
            )
    conn.commit()


def spec_digest(spec_json: str) -> str:
    """SHA-256 (hex) of an asset row's stored ``spec_json`` — the content a history row binds."""
    return hashlib.sha256(spec_json.encode("utf-8")).hexdigest()


def record_status_change(
    conn: sqlite3.Connection,
    *,
    asset_name: str,
    version: str,
    from_status: str | None,
    to_status: str,
    changed_at: str,
    changed_by: str,
    via: str,
    asset_id: str,
    spec_sha256: str,
) -> None:
    """Append one lifecycle transition. The caller commits (same transaction).

    *asset_id* and *spec_sha256* bind the row to one registration: the assets row id
    and :func:`spec_digest` of its ``spec_json``. They are required so that no new
    writer can produce an unbound row, which a re-registration of the same
    name@version could otherwise borrow as "previously in production".
    """
    conn.execute(
        """
        INSERT INTO asset_status_history
            (asset_name, version, from_status, to_status, changed_at, changed_by, via,
             asset_id, spec_sha256)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            asset_name, version, from_status, to_status, changed_at, changed_by, via,
            asset_id, spec_sha256,
        ),
    )


def _ensure_additive_columns(conn: sqlite3.Connection) -> None:
    for table, column, col_type in _ADDITIVE_COLUMNS:
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
    conn.commit()
