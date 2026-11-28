"""``nova doctor --check-storage`` is strictly read-only.

It used to open the SQLite registry through the store's ``get_connection``, which
creates the parent directory, switches the file to WAL (rewriting its header) and
leaves ``-wal`` / ``-shm`` sidecars. A diagnostic must leave the directory byte for
byte as it found it, and report a missing database as "would be created" rather
than create it. Same read-only URI as ``nova doctor --check-server``.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from novafabric.cli.main import app
from novafabric.storage import SQLiteBackend
from novafabric.storage._sqlite_ro import sqlite_ro_uri

runner = CliRunner()


def _snapshot(root: Path) -> dict[str, tuple[int, str, int]]:
    """Every path under *root* with (size, sha256, mtime_ns); directories too."""
    out: dict[str, tuple[int, str, int]] = {}
    for path in sorted(root.rglob("*")):
        st = path.stat()
        digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "dir"
        out[str(path.relative_to(root))] = (st.st_size, digest, st.st_mtime_ns)
    return out


def _journal_mode(db: Path) -> str:
    conn = sqlite3.connect(str(db))
    try:
        return str(conn.execute("PRAGMA journal_mode").fetchone()[0])
    finally:
        conn.close()


def _rollback_journal_db(db: Path) -> None:
    conn = sqlite3.connect(str(db))
    conn.executescript(
        "CREATE TABLE schema_version (version INTEGER PRIMARY KEY);"
        "INSERT INTO schema_version VALUES (3);"
        "CREATE TABLE assets (id TEXT PRIMARY KEY);"
        "INSERT INTO assets VALUES ('a1'), ('a2');"
    )
    conn.commit()
    conn.close()


def _check_storage(db: Path) -> object:
    return runner.invoke(
        app, ["doctor", "--check-storage", "--db-path", str(db)], env={"COLUMNS": "200"}
    )


def test_existing_rollback_journal_db_is_not_touched(tmp_path: Path) -> None:
    store = tmp_path / "store"
    store.mkdir()
    db = store / "registry.db"
    _rollback_journal_db(db)
    before = _snapshot(store)

    result = _check_storage(db)

    assert result.exit_code == 0, result.output
    assert "Schema version" in result.output and "3" in result.output
    assert "assets" in result.output
    assert _snapshot(store) == before  # no -wal, no -shm, header unchanged
    assert _journal_mode(db) == "delete"  # not converted to WAL


def test_existing_cleanly_closed_wal_db_gets_no_sidecars(tmp_path: Path) -> None:
    from novafabric.registry.store import get_connection

    store = tmp_path / "store"
    store.mkdir()
    db = store / "registry.db"
    conn = get_connection(db)  # the real writer path: WAL
    conn.execute("CREATE TABLE assets (id TEXT PRIMARY KEY)")
    conn.execute("INSERT INTO assets VALUES ('a1')")
    conn.commit()
    conn.close()  # last close checkpoints and removes the sidecars
    assert not Path(f"{db}-wal").exists()
    before = _snapshot(store)

    result = _check_storage(db)

    assert result.exit_code == 0, result.output
    assert _snapshot(store) == before


def test_missing_db_is_reported_as_would_be_created_and_not_created(tmp_path: Path) -> None:
    parent = tmp_path / "not-yet"
    db = parent / "registry.db"

    result = _check_storage(db)

    assert result.exit_code == 0, result.output
    assert "would be created on first use" in result.output
    assert not parent.exists()  # no mkdir, no database file


def test_default_location_under_a_missing_home_creates_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "fresh-home"
    monkeypatch.setenv("NOVAFABRIC_HOME", str(home))
    monkeypatch.delenv("NOVAFABRIC_DB_PATH", raising=False)

    result = runner.invoke(app, ["doctor", "--check-storage"], env={"COLUMNS": "200"})

    assert result.exit_code == 0, result.output
    assert "would be created on first use" in result.output
    assert not (home / "registry.db").exists()


def test_not_a_database_fails_and_leaves_the_file_alone(tmp_path: Path) -> None:
    store = tmp_path / "store"
    store.mkdir()
    db = store / "registry.db"
    db.write_bytes(b"this is not an sqlite file, just bytes" * 10)
    before = _snapshot(store)

    result = _check_storage(db)

    assert result.exit_code == 1, result.output
    assert isinstance(result.exception, SystemExit)
    assert _snapshot(store) == before


def test_backend_info_marks_existence(tmp_path: Path) -> None:
    missing = SQLiteBackend(tmp_path / "missing.db").info()
    assert missing.db_exists is False
    db = tmp_path / "registry.db"
    _rollback_journal_db(db)
    present = SQLiteBackend(db).info()
    assert present.db_exists is True
    assert present.schema_version == 3
    assert present.row_counts["assets"] == 2


def test_ro_uri_uses_immutable_only_without_sidecars(tmp_path: Path) -> None:
    db = tmp_path / "registry.db"
    _rollback_journal_db(db)
    assert sqlite_ro_uri(db).endswith("?mode=ro&immutable=1")
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(f"{db}{suffix}")
        sidecar.write_bytes(b"")
        try:
            assert sqlite_ro_uri(db).endswith("?mode=ro"), suffix
        finally:
            os.unlink(sidecar)


def test_check_storage_help_says_read_only() -> None:
    from typer.main import get_command

    doctor = get_command(app).commands["doctor"]  # type: ignore[attr-defined]
    option = next(p for p in doctor.params if p.name == "check_storage")
    assert "Read-only" in (option.help or "")
    assert "would be created on first use" in (option.help or "")
