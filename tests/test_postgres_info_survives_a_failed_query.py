"""One failed query does not erase every later row count (review storage-backup-18).

psycopg3 runs these queries in one implicit transaction. A missing ``schema_version``
table aborted it, every later ``SELECT COUNT(*)`` raised ``InFailedSqlTransaction``,
and ``except Exception: pass`` swallowed them: ``nova doctor`` and the support bundle
reported ``{}`` for a healthy database. The fake below has that aborted-transaction
behaviour.
"""

from __future__ import annotations

from typing import Any

from novafabric.storage import _postgres


class _Aborted(Exception):
    pass


class _Conn:
    def __init__(self) -> None:
        self.aborted = False

    def execute(self, sql: str) -> Any:
        if self.aborted:
            raise _Aborted("current transaction is aborted")
        if "schema_version" in sql:
            self.aborted = True
            raise _Aborted('relation "schema_version" does not exist')
        return type("Cur", (), {"fetchone": lambda self: (3,)})()

    def rollback(self) -> None:
        self.aborted = False


def test_counts_survive_a_missing_schema_version_table() -> None:
    conn = _Conn()
    assert _postgres._read_schema_version(conn) is None
    assert _postgres._read_row_counts(conn) == dict.fromkeys(_postgres._KNOWN_TABLES, 3)
