"""Tests for the shared SQLite connect helper (enterprise-hardening 4.1)."""

from __future__ import annotations

from pathlib import Path

from novafabric._sqlite_util import DEFAULT_BUSY_TIMEOUT_MS, connect_sqlite


def test_busy_timeout_is_set(tmp_path: Path) -> None:
    conn = connect_sqlite(tmp_path / "t.db")
    try:
        (value,) = conn.execute("PRAGMA busy_timeout").fetchone()
        assert value == DEFAULT_BUSY_TIMEOUT_MS
    finally:
        conn.close()


def test_custom_busy_timeout(tmp_path: Path) -> None:
    conn = connect_sqlite(tmp_path / "t.db", busy_timeout_ms=1234)
    try:
        (value,) = conn.execute("PRAGMA busy_timeout").fetchone()
        assert value == 1234
    finally:
        conn.close()


def test_connection_is_usable(tmp_path: Path) -> None:
    conn = connect_sqlite(tmp_path / "t.db", check_same_thread=False)
    try:
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.execute("INSERT INTO t VALUES (1)")
        conn.commit()
        assert conn.execute("SELECT x FROM t").fetchone()[0] == 1
    finally:
        conn.close()
