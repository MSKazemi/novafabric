"""The content-index schema appears all at once, and a partial one never breaks a delete.

``executescript`` commits each statement on its own, so the three content-index
tables used to become visible one by one. A capsule delete that ran while the
serve-startup indexer was still creating them saw ``capsule_docs`` without
``capsule_index_state`` and failed with ``no such table: capsule_index_state``
(an intermittent 500 on ``DELETE /api/runs/{id}``, 2026-10-09).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from novafabric.query import content_index as ci

pytestmark = pytest.mark.skipif(not ci.fts5_available(), reason="needs SQLite FTS5")


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_delete_completes_a_partial_schema_instead_of_failing(tmp_path: Path) -> None:
    conn = sqlite3.connect(tmp_path / "registry.db")
    # What another connection could observe mid-creation (or an older install):
    conn.execute(
        "CREATE TABLE capsule_docs (doc_id INTEGER PRIMARY KEY, run_id TEXT NOT NULL,"
        " stream TEXT NOT NULL, ref TEXT NOT NULL, line_no INTEGER, text TEXT NOT NULL)"
    )
    conn.commit()
    ci.delete_run(conn, "run-x")  # used to raise: no such table capsule_index_state
    assert {"capsule_docs", "capsule_index_state"} <= _tables(conn)


def test_a_failed_schema_creation_leaves_no_partial_tables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = sqlite3.connect(tmp_path / "registry.db")
    monkeypatch.setattr(ci, "_DDL", ci._DDL + "\nCREATE TABLE broken (;\n")
    with pytest.raises(sqlite3.OperationalError):
        ci.ensure_content_index(conn)
    assert not {"capsule_docs", "capsule_index_state"} & _tables(conn)


def test_another_connection_never_sees_half_the_schema(tmp_path: Path) -> None:
    db = tmp_path / "registry.db"
    writer = sqlite3.connect(db)
    reader = sqlite3.connect(db, timeout=0.05)
    ci.ensure_content_index(writer)
    present = {"capsule_docs", "capsule_index_state"} & _tables(reader)
    assert present in (set(), {"capsule_docs", "capsule_index_state"})
    ci.delete_run(reader, "run-y")  # a full schema: deleting a missing run is a no-op


def test_delete_without_any_content_index_is_a_no_op(tmp_path: Path) -> None:
    conn = sqlite3.connect(tmp_path / "registry.db")
    ci.delete_run(conn, "run-z")
    assert not {"capsule_docs", "capsule_index_state"} & _tables(conn)
