"""Server-side runs-cache index access (ADR-0206 P1, experimental).

``GET /v0/capsules`` keyset pagination is backed by the existing
:mod:`novafabric.registry.runs_cache` SQLite index — the same schema and
seek query the serve dashboard uses. This module owns the server's access
to it:

* :func:`open_index` — connect to the registry DB and ensure the table;
* :func:`sync_index` — lazy backfill (O(new capsules)) plus pruning of rows
  whose capsule directory has vanished. The capsule directory stays the
  source of truth; the index is derived and rebuildable, never
  authoritative (``metadata_store/interface.py`` doctrine);
* :func:`upsert_capsule` — upload-time row upsert + content indexing;
* :func:`remove_run` — delete-time cleanup of the runs-cache row and the
  content-search index rows (ADR-0204 per-run delete contract).

All callers treat failures of the *content* index as non-fatal (fail-open,
matching ``maybe_index_capsule``); runs-cache failures surface normally.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from novafabric._paths import registry_db_path
from novafabric.query.content_index import delete_run as _content_delete_run
from novafabric.query.content_index import maybe_index_capsule
from novafabric.registry.runs_cache import (
    build_runs_index,
    ensure_runs_cache,
    upsert_run,
)


def open_index(db_path: Path | None) -> sqlite3.Connection:
    """Open the registry DB (default path when *db_path* is None).

    Ensures the ``runs_cache`` table exists. Caller closes the connection.
    """
    path = db_path if db_path is not None else registry_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    ensure_runs_cache(conn)
    return conn


def sync_index(conn: sqlite3.Connection, capsule_dir: Path) -> None:
    """Reconcile the index with the capsule directory (lazy backfill + prune).

    Backfill is incremental — only capsule dirs not yet indexed get their
    ``capsule.yaml`` parsed, so a steady-state page request re-reads zero
    manifests. Rows whose backing directory no longer exists (out-of-band
    deletion) are pruned, including their content-index rows.
    """
    build_runs_index(capsule_dir, conn, incremental=True)
    rows = conn.execute("SELECT run_id, capsule_path FROM runs_cache").fetchall()
    stale: list[str] = []
    for row in rows:
        run_id, capsule_path = row["run_id"], row["capsule_path"]
        backing = (
            Path(capsule_path) if capsule_path else capsule_dir / str(run_id)
        )
        if not (backing / "capsule.yaml").exists():
            stale.append(str(run_id))
    for run_id in stale:
        remove_run(conn, run_id)
    if stale:
        conn.commit()


def upsert_capsule(
    conn: sqlite3.Connection,
    capsule_dir: Path,
    run_id: str,
    manifest: dict[str, Any],
) -> None:
    """Index one freshly-uploaded capsule (row upsert + content index)."""
    dest = capsule_dir / run_id
    summary = {
        "run_id": run_id,
        "status": manifest.get("status"),
        "created_at": manifest.get("created_at"),
        "finished_at": manifest.get("finished_at"),
        "duration_ms": manifest.get("duration_ms"),
        "exit_code": manifest.get("exit_code"),
        "model_call_count": manifest.get("model_call_count", 0),
        "tool_call_count": manifest.get("tool_call_count", 0),
        "mutating_tool_count": manifest.get("mutating_tool_count", 0),
        "command": manifest.get("command"),
        "novafabric_version": manifest.get("novafabric_version"),
        "capsule_path": str(dest.resolve()),
    }
    upsert_run(conn, summary)
    maybe_index_capsule(conn, dest, run_id)  # fail-open by contract
    conn.commit()


def remove_run(conn: sqlite3.Connection, run_id: str) -> None:
    """Remove a run's derived rows: runs_cache + content-search index."""
    conn.execute("DELETE FROM runs_cache WHERE run_id = ?", (run_id,))
    _content_delete_run(conn, run_id)  # idempotent; no-op without tables
