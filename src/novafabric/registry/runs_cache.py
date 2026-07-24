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
CREATE INDEX IF NOT EXISTS idx_runs_cache_created_day
    ON runs_cache(substr(created_at, 1, 10));
"""

# Success semantics used by the report builders since v0.30: a run counts as a
# success when the process exited 0 or the capsule recorded status "ok".
_SUCCESS_SQL = "(exit_code = 0 OR status = 'ok')"


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
        # O(new) invariant (ADR-0206): an already-indexed capsule dir is
        # skipped *before* its manifest is parsed. Dir name == run_id for
        # every in-tree writer; the post-parse check below still covers the
        # exotic mismatch case.
        if incremental and d.name in existing:
            continue
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
        # ADR-0204 (experimental): content-index the redacted capsule text.
        # Fail-open — never blocks runs_cache population.
        from novafabric.query.content_index import maybe_index_capsule  # noqa: PLC0415

        maybe_index_capsule(conn, d, str(run_id))
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
    after: tuple[str | None, str] | None = None,
    agent: str | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Return (page, total_count) from the index.

    Filters are applied at the SQL layer so only matching rows are read.
    Returns empty lists when the cache is unpopulated (caller must fall back).

    ``after`` enables keyset (cursor) pagination: only rows strictly after
    the ``(created_at, run_id)`` position in the DESC sort order are
    returned, and ``offset`` is ignored. O(page) instead of O(offset).
    Rows with NULL ``created_at`` sort last under ``created_at DESC``
    (SQLite NULLS-first-ASC ⇒ NULLS-last-DESC); an ``after`` whose
    ``created_at`` element is None seeks within that tail by ``run_id``
    alone (ADR-0206 D1 / spec "NULL ordering").
    """
    where_parts = ["1=1"]
    params: list[Any] = []
    if after is not None:
        if after[0] is None:
            # Cursor already in the NULL created_at tail: page by run_id.
            where_parts.append("(created_at IS NULL AND run_id < ?)")
            params.append(after[1])
        else:
            # DESC keyset: strictly older than the cursor position — plus the
            # NULL tail, which sorts after every non-NULL created_at.
            where_parts.append(
                "((created_at, run_id) < (?, ?) OR created_at IS NULL)"
            )
            params.extend([after[0], after[1]])
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
    if agent:
        # Substring match on the stored command tokens (run-history report
        # filter). SQL-side so keyset pages stay full-sized.
        where_parts.append("LOWER(command_json) LIKE ?")
        params.append(f"%{agent.lower()}%")

    where = " AND ".join(where_parts)
    total = conn.execute(
        f"SELECT COUNT(*) FROM runs_cache WHERE {where}", params
    ).fetchone()[0]
    rows = conn.execute(
        f"SELECT * FROM runs_cache WHERE {where} ORDER BY created_at DESC, run_id DESC"
        f" LIMIT ? OFFSET ?",
        [*params, limit, 0 if after is not None else offset],
    ).fetchall()
    out = []
    for row in rows:
        d = dict(row)
        cmd = d.pop("command_json", None)
        d["command"] = json.loads(cmd) if cmd else []
        out.append(d)
    return out, total


def _window_where(
    since: str | None, until: str | None
) -> tuple[str, list[Any]]:
    """Build the shared created_at window predicate.

    ``until`` compares on the date prefix so a bare date covers its whole day
    (same convention as the analytics summary endpoint).
    """
    where = ["created_at IS NOT NULL"]
    params: list[Any] = []
    if since:
        where.append("created_at >= ?")
        params.append(since)
    if until:
        where.append("substr(created_at, 1, 10) <= ?")
        params.append(until[:10])
    return " AND ".join(where), params


def aggregate_runs_daily(
    conn: sqlite3.Connection,
    *,
    since: str | None = None,
    until: str | None = None,
    failed_predicate: str = "status IS NOT NULL AND status != 'success'",
) -> list[dict[str, Any]]:
    """Day-bucketed run aggregates straight from SQL (ADR-0199 rule 1).

    One indexed GROUP BY over the ``substr(created_at, 1, 10)`` expression
    index; never materializes row lists.
    """
    where_sql, params = _window_where(since, until)
    rows = conn.execute(
        f"""
        SELECT substr(created_at, 1, 10) AS bucket,
               COUNT(*) AS run_count,
               SUM(CASE WHEN {failed_predicate} THEN 1 ELSE 0 END) AS failed_count,
               SUM(COALESCE(model_call_count, 0)) AS model_call_count,
               SUM(COALESCE(tool_call_count, 0)) AS tool_call_count,
               MAX(duration_ms) AS duration_ms_max
        FROM runs_cache
        WHERE {where_sql}
        GROUP BY bucket
        ORDER BY bucket
        """,
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def durations_by_bucket(
    conn: sqlite3.Connection,
    *,
    prefix_len: int = 10,
    since: str | None = None,
    until: str | None = None,
) -> list[tuple[str, float]]:
    """(bucket, duration_ms) pairs ordered by bucket then duration.

    Feeds percentile computation; bounded by the requested window.
    """
    where_sql, params = _window_where(since, until)
    rows = conn.execute(
        f"""
        SELECT substr(created_at, 1, ?) AS bucket, duration_ms
        FROM runs_cache
        WHERE {where_sql} AND duration_ms IS NOT NULL
        ORDER BY bucket, duration_ms
        """,
        [prefix_len, *params],
    ).fetchall()
    return [(r[0], float(r[1])) for r in rows]


def aggregate_runs_windowed(
    conn: sqlite3.Connection,
    *,
    prefix_len: int = 10,
    since: str | None = None,
    until: str | None = None,
) -> list[dict[str, Any]]:
    """Window-bucketed success/failure counts for the throughput report.

    ``prefix_len`` selects the window: 13 = hour, 10 = day, 7 = week-ish
    (year-month), matching the report builder's historical prefixes.
    """
    where_sql, params = _window_where(since, until)
    rows = conn.execute(
        f"""
        SELECT substr(created_at, 1, ?) AS window,
               COUNT(*) AS runs,
               SUM(CASE WHEN {_SUCCESS_SQL} THEN 1 ELSE 0 END) AS successes
        FROM runs_cache
        WHERE {where_sql}
        GROUP BY window
        ORDER BY window
        """,
        [prefix_len, *params],
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["failures"] = d["runs"] - (d["successes"] or 0)
        d["successes"] = d["successes"] or 0
        out.append(d)
    return out


def aggregate_runs_by_agent(
    conn: sqlite3.Connection,
    *,
    since: str | None = None,
    until: str | None = None,
) -> list[dict[str, Any]]:
    """Per-agent (first command token) totals for the cost-burn report.

    Uses the built-in JSON1 ``json_extract`` to read the first element of the
    stored ``command_json`` array in SQL; NULL/empty commands group under
    ``(unknown)``.
    """
    where_sql, params = _window_where(since, until)
    rows = conn.execute(
        f"""
        SELECT COALESCE(json_extract(command_json, '$[0]'), '(unknown)') AS agent,
               COUNT(*) AS runs,
               SUM(COALESCE(model_call_count, 0)) AS model_calls,
               SUM(COALESCE(tool_call_count, 0)) AS tool_calls
        FROM runs_cache
        WHERE {where_sql}
        GROUP BY agent
        ORDER BY runs DESC
        """,
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def runs_totals(
    conn: sqlite3.Connection,
    *,
    since: str | None = None,
    until: str | None = None,
) -> dict[str, int]:
    """One-row whole-window totals for the executive-summary report."""
    where_sql, params = _window_where(since, until)
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS total_runs,
               SUM(CASE WHEN {_SUCCESS_SQL} THEN 1 ELSE 0 END) AS successes,
               SUM(COALESCE(model_call_count, 0)) AS model_calls,
               SUM(COALESCE(tool_call_count, 0)) AS tool_calls
        FROM runs_cache
        WHERE {where_sql}
        """,
        params,
    ).fetchone()
    total = int(row[0] or 0)
    successes = int(row[1] or 0)
    return {
        "total_runs": total,
        "successes": successes,
        "failures": total - successes,
        "model_calls": int(row[2] or 0),
        "tool_calls": int(row[3] or 0),
    }


def get_run_summary(
    conn: sqlite3.Connection, run_id: str
) -> dict[str, Any] | None:
    """Return the cached summary for a single run, or ``None`` if absent.

    Used by the run-detail endpoint to degrade gracefully when a capsule
    directory is missing on disk but the run's metadata is still indexed.
    """
    row = conn.execute(
        "SELECT * FROM runs_cache WHERE run_id = ?", (run_id,)
    ).fetchone()
    if row is None:
        return None
    d = dict(row)
    cmd = d.pop("command_json", None)
    d["command"] = json.loads(cmd) if cmd else []
    return d


def count_cached_runs(conn: sqlite3.Connection) -> int:
    """Return total rows in runs_cache. Zero means the cache is unpopulated."""
    return int(conn.execute("SELECT COUNT(*) FROM runs_cache").fetchone()[0])
