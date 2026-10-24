"""The pg backup's row counts come from the dump's own snapshot (review storage-backup-08).

``pg_dump`` ran first and the dump-time row counts were taken afterwards on a new
connection, so on a busy server every count was taken after the dump. Restoring a
correct dump then reported a count mismatch. The counts are now taken inside one
``REPEATABLE READ`` transaction whose exported snapshot ``pg_dump --snapshot`` uses.

No Postgres here: a fake ``psycopg`` records the order of operations.
"""

from __future__ import annotations

import subprocess
import sys
import types
from pathlib import Path
from typing import Any
from unittest.mock import patch

events: list[str] = []


class _Cursor:
    def __init__(self, row: tuple[Any, ...]) -> None:
        self._row = row

    def fetchone(self) -> tuple[Any, ...]:
        return self._row


class _Tx:
    def __enter__(self) -> _Tx:
        events.append("savepoint")
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class _Conn:
    isolation_level: Any = None

    def __enter__(self) -> _Conn:
        return self

    def __exit__(self, *exc: object) -> None:
        events.append("close")

    def execute(self, sql: str) -> _Cursor:
        events.append(f"sql:{sql}:{self.isolation_level}")
        if "pg_export_snapshot" in sql:
            return _Cursor(("00000003-0000001B-1",))
        if "alembic_version" in sql:
            return _Cursor(("rev42",))
        return _Cursor((7,))

    def transaction(self) -> _Tx:
        return _Tx()

    def commit(self) -> None:
        events.append("commit")

    def rollback(self) -> None:
        events.append("rollback")


def _fake_psycopg() -> types.ModuleType:
    mod = types.ModuleType("psycopg")
    mod.Error = type("Error", (Exception,), {})  # type: ignore[attr-defined]
    mod.IsolationLevel = types.SimpleNamespace(REPEATABLE_READ="REPEATABLE_READ")  # type: ignore[attr-defined]
    mod.connect = lambda dsn: _Conn()  # type: ignore[attr-defined]
    return mod


def test_counts_and_dump_share_one_snapshot(tmp_path: Path) -> None:
    from novafabric.backup import create

    events.clear()

    def fake_run(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        events.append("pg_dump " + " ".join(a for a in argv if a.startswith("--snapshot")))
        Path(argv[argv.index("--file") + 1]).write_bytes(b"PGDMP")
        return subprocess.CompletedProcess(argv, 0, "", "")

    with patch.dict(sys.modules, {"psycopg": _fake_psycopg()}), \
            patch.object(create.shutil, "which", return_value="/usr/bin/pg_dump"), \
            patch.object(create.subprocess, "run", side_effect=fake_run):
        revision, counts = create._dump_with_consistent_counts(
            "postgresql://nova@db/nova", tmp_path / "d.dump"
        )

    assert revision == "rev42"
    assert counts == dict.fromkeys(create.PG_COUNT_TABLES, 7)
    dump = events.index("pg_dump --snapshot=00000003-0000001B-1")
    count_sql = [i for i, e in enumerate(events) if "count(*)" in e]
    assert count_sql and all(i < dump for i in count_sql)  # counted in the snapshot...
    assert all("REPEATABLE_READ" in events[i] for i in count_sql)
    assert events.index("commit") > dump  # ...which stayed open for pg_dump


def test_without_psycopg_the_dump_still_runs(tmp_path: Path) -> None:
    from novafabric.backup import create

    def fake_run(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert not any(a.startswith("--snapshot") for a in argv)
        Path(argv[argv.index("--file") + 1]).write_bytes(b"PGDMP")
        return subprocess.CompletedProcess(argv, 0, "", "")

    with patch.dict(sys.modules, {"psycopg": None}), \
            patch.object(create.shutil, "which", return_value="/usr/bin/pg_dump"), \
            patch.object(create.subprocess, "run", side_effect=fake_run):
        assert create._dump_with_consistent_counts("postgresql://nova@db/nova", tmp_path / "d") == (None, None)
    assert (tmp_path / "d").read_bytes() == b"PGDMP"
