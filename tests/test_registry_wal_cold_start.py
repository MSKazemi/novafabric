"""``get_connection`` must not fail when several threads open one cold database.

``PRAGMA journal_mode=WAL`` is a write needing a brief exclusive lock, and SQLite
does not apply the busy timeout to a journal-mode change — it returns SQLITE_BUSY
immediately. Issuing it on *every* connect made concurrent opens raise
``OperationalError: database is locked``: measured 25 failures in 500 trials of
eight threads opening one fresh database, and 1 failure in 8 full ``make test-fast``
runs on a byte-identical tree, surfacing as
``tests/serve/test_schema_init_once.py`` failing in its *setup* rather than on the
assertion it exists to make.

The rate is far too low to gate on directly, so these tests pin the two
deterministic properties that make the rate zero instead.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from novafabric.registry import store


def test_second_connection_issues_no_journal_mode_write(tmp_path: Path) -> None:
    """The journal mode is a persistent property of the file, so reading beats writing.

    This is the property that removes the contention: every connection after the
    first must not attempt the write at all.
    """
    db = tmp_path / "wal.db"
    store.get_connection(db).close()  # first connection establishes WAL

    statements: list[str] = []
    conn = sqlite3.connect(str(db))
    conn.set_trace_callback(statements.append)
    try:
        store._ensure_wal(conn)
    finally:
        conn.set_trace_callback(None)
        conn.close()

    writes = [s for s in statements if "journal_mode" in s.lower() and "=" in s]
    assert writes == [], f"second connection still wrote the journal mode: {writes}"
    assert statements, "trace callback captured nothing — the assertion above is vacuous"


class _RefusesWalWrite:
    """Duck-typed stand-in: ``_ensure_wal`` uses only ``execute`` and ``commit``.

    ``sqlite3.Connection`` is a C type and rejects attribute assignment, so the
    refusal has to be injected by delegation rather than by monkeypatching.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def execute(self, sql: str, *a: object, **k: object) -> sqlite3.Cursor:
        if "journal_mode=wal" in sql.lower().replace(" ", ""):
            raise sqlite3.OperationalError("database is locked")
        return self._conn.execute(sql, *a, **k)

    def commit(self) -> None:
        self._conn.commit()


def test_losing_the_race_to_another_writer_is_not_an_error(tmp_path: Path) -> None:
    """The retry's exit condition is the invariant, not 'my statement succeeded'.

    A thread whose ``PRAGMA journal_mode=WAL`` is refused, but whose database is
    already in WAL because another thread won, has nothing left to do.
    """
    db = tmp_path / "wal.db"
    store.get_connection(db).close()  # already WAL, as if a rival thread had won
    conn = sqlite3.connect(str(db))
    try:
        store._ensure_wal(_RefusesWalWrite(conn))  # type: ignore[arg-type]
    finally:
        conn.close()


def test_a_persistently_locked_journal_mode_still_raises(tmp_path: Path) -> None:
    """Retrying is bounded, and WAL is never silently abandoned.

    Callers that expect concurrent readers must not be handed a connection in a
    mode that does not allow them, so exhausting the retries is an error.
    """
    db = tmp_path / "cold.db"
    conn = sqlite3.connect(str(db))
    try:
        with pytest.raises(sqlite3.OperationalError):
            store._ensure_wal(_RefusesWalWrite(conn))  # type: ignore[arg-type]
    finally:
        conn.close()


def test_eight_threads_opening_one_cold_database_all_succeed(tmp_path: Path) -> None:
    """The end-to-end shape of the original failure, at a size that stays fast.

    One trial reproduced the bug only ~5 % of the time, so this is a smoke test of
    the fixed path rather than the guard — the guard is the three tests above.
    """
    db = tmp_path / "race.db"
    errors: list[BaseException] = []
    barrier = threading.Barrier(8)

    def worker() -> None:
        try:
            barrier.wait(timeout=10)
            store.get_connection(db).close()
        except BaseException as exc:  # noqa: BLE001 - reported, not swallowed
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, f"worker raised: {errors!r}"
