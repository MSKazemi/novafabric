"""Lightweight runs index stored in registry.db.

``nova serve`` populates this table from capsule.yaml files on startup and
keeps it current via the stats-refresh background thread.  All dashboard
``/api/runs`` queries read from here instead of doing O(N) disk scans.

The table is a *cache* — it is always rebuildable from the capsule filesystem
by calling :func:`build_runs_index`.  It must never be the source of truth.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

_DDL = """
CREATE TABLE IF NOT EXISTS runs_cache (
    run_id              TEXT PRIMARY KEY,
    status              TEXT,
    created_at          TEXT,
    finished_at         TEXT,
    duration_ms         REAL,
    exit_code           INTEGER,
    model_call_count    INTEGER DEFAULT 0,
    tool_call_count     INTEGER DEFAULT 0,
    mutating_tool_count INTEGER DEFAULT 0,
    command_json        TEXT,
    novafabric_version  TEXT,
    capsule_path        TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_cache_created_at
    ON runs_cache(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_cache_status
    ON runs_cache(status);
CREATE INDEX IF NOT EXISTS idx_runs_cache_status_created_at
    ON runs_cache(status, created_at DESC);
"""


def ensure_runs_cache(conn: sqlite3.Connection) -> None:
    """Create runs_cache table + indexes if they do not exist. Idempotent."""
    conn.executescript(_DDL)


def upsert_run(conn: sqlite3.Connection, summary: dict[str, Any]) -> None:
    """Insert or replace a run summary row. ``summary`` is a dict from
    :func:`~novafabric.serve.capsule_loader.list_run_summaries`.
    """
    cmd = summary.get("command")
    conn.execute(
        """
        INSERT OR REPLACE INTO runs_cache (
            run_id, status, created_at, finished_at, duration_ms,
            exit_code, model_call_count, tool_call_count, mutating_tool_count,
            command_json, novafabric_version, capsule_path
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            summary.get("run_id"),
            summary.get("status"),
            summary.get("created_at"),
            summary.get("finished_at"),
            summary.get("duration_ms"),
            summary.get("exit_code"),
            summary.get("model_call_count", 0),
            summary.get("tool_call_count", 0),
            summary.get("mutating_tool_count", 0),
            json.dumps(cmd) if cmd is not None else None,
            summary.get("novafabric_version"),
            summary.get("capsule_path"),
        ),
    )


def build_runs_index(
    capsule_dir: Path,
    conn: sqlite3.Connection,
    *,
    incremental: bool = True,
) -> int:
    """Populate runs_cache from the capsule filesystem.

    When ``incremental=True`` (default), only capsule directories not yet
    in the cache are scanned — making subsequent calls O(new_runs).
    Returns the number of rows inserted.
    """
    import yaml  # noqa: PLC0415

    from novafabric.serve.capsule_loader import (  # noqa: PLC0415
        discover_capsule_dirs,
        load_capsule_manifest,
    )

    existing: set[str] = set()
    if incremental:
        rows = conn.execute("SELECT run_id FROM runs_cache").fetchall()
        existing = {r[0] for r in rows}

    inserted = 0
    for d in discover_capsule_dirs(capsule_dir):
        try:
            m = load_capsule_manifest(d)
        except (FileNotFoundError, yaml.YAMLError):
            continue
        run_id = m.get("run_id", d.name)
        if incremental and run_id in existing:
            continue
        cmd = m.get("command", [])
        conn.execute(
            """
            INSERT OR REPLACE INTO runs_cache (
                run_id, status, created_at, finished_at, duration_ms,
                exit_code, model_call_count, tool_call_count, mutating_tool_count,
                command_json, novafabric_version, capsule_path
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                run_id,
                m.get("status"),
                m.get("created_at"),
                m.get("finished_at"),
                m.get("duration_ms"),
                m.get("exit_code"),
                m.get("model_call_count", 0),
                m.get("tool_call_count", 0),
                m.get("mutating_tool_count", 0),
                json.dumps(cmd) if cmd else None,
                m.get("novafabric_version"),
                str(d.resolve()),
            ),
        )
        inserted += 1
    conn.commit()
    return inserted


def query_runs(
    conn: sqlite3.Connection,
    *,
    limit: int = 100,
    offset: int = 0,
    since: str | None = None,
    until: str | None = None,
    status: str | None = None,
    q: str | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Return (page, total_count) from the index.

    Filters are applied at the SQL layer so only matching rows are read.
    Returns empty lists when the cache is unpopulated (caller must fall back).
    """
    where_parts = ["1=1"]
    params: list[Any] = []
    if since:
        where_parts.append("created_at >= ?")
        params.append(since)
    if until:
        where_parts.append("created_at <= ?")
        params.append(until)
    if status and status != "all":
        where_parts.append("status = ?")
        params.append(status)
    if q:
        where_parts.append(
            "(LOWER(run_id) LIKE ? OR LOWER(command_json) LIKE ?)"
        )
        pattern = f"%{q.lower()}%"
        params.extend([pattern, pattern])

    where = " AND ".join(where_parts)
    total = conn.execute(
        f"SELECT COUNT(*) FROM runs_cache WHERE {where}", params
    ).fetchone()[0]
    rows = conn.execute(
        f"SELECT * FROM runs_cache WHERE {where} ORDER BY created_at DESC, run_id DESC"
        f" LIMIT ? OFFSET ?",
        [*params, limit, offset],
    ).fetchall()
    out = []
    for row in rows:
        d = dict(row)
        cmd = d.pop("command_json", None)
        d["command"] = json.loads(cmd) if cmd else []
        out.append(d)
    return out, total


def count_cached_runs(conn: sqlite3.Connection) -> int:
    """Return total rows in runs_cache. Zero means the cache is unpopulated."""
    return int(conn.execute("SELECT COUNT(*) FROM runs_cache").fetchone()[0])
