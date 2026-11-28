"""Read-only SQLite access that never creates, converts or locks anything.

Shared by ``nova doctor --check-server`` (the server deployment preflight) and
``nova doctor --check-storage`` (``SQLiteBackend.info``). A diagnostic must leave
the directory exactly as it found it: no parent ``mkdir``, no new database file, no
``-wal`` / ``-shm`` sidecars, and no ``PRAGMA journal_mode=WAL`` conversion — the
registry store's ``get_connection`` does all four, which is right for a writer and
wrong for a check.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from novafabric.server import schema_skew


def sqlite_ro_uri(path: Path) -> str:
    """A read-only SQLite URI for *path* that creates no ``-wal`` / ``-shm`` file.

    The registry is always WAL. ``mode=ro`` alone still creates both sidecars when
    they are absent (and under ``sudo`` they would be root-owned, locking the service
    user out). When neither sidecar exists the last writer closed cleanly and
    checkpointed, so ``immutable=1`` reads the complete database without them. When
    they exist, nothing is created and plain ``mode=ro`` sees uncheckpointed pages.

    A rollback-journal database (one never opened through the registry store) has no
    WAL sidecars either; a leftover ``-journal`` is treated like a sidecar so a hot
    journal is never bypassed by ``immutable=1``.
    """
    sidecars = (Path(f"{path}-wal"), Path(f"{path}-shm"), Path(f"{path}-journal"))
    query = "mode=ro" if any(p.exists() for p in sidecars) else "mode=ro&immutable=1"
    return f"{path.resolve().as_uri()}?{query}"


def compare_sqlite_read_only(path: Path) -> schema_skew.SkewReport:
    """``schema_skew.compare_revisions("sqlite", path)`` without touching the directory.

    Same five states and the same packaged script head; only the stamp read differs
    (:func:`sqlite_ro_uri`). The server's own start-up comparator is unchanged.
    """
    import sqlite3

    from novafabric.migrations import registry_track
    from novafabric.server import schema_skew

    def report(
        status: schema_skew.SkewStatus, stamp: str | None, head: str | None, detail: str
    ) -> schema_skew.SkewReport:
        return schema_skew.SkewReport(
            status=status,
            backend="sqlite",
            track="registry",
            db_revision=stamp,
            head_revision=head,
            detail=detail,
        )

    head = registry_track.script_head("sqlite")
    try:
        conn = sqlite3.connect(sqlite_ro_uri(path), uri=True, timeout=2)
        try:
            row = conn.execute("SELECT version_num FROM alembic_version LIMIT 1").fetchone()
        finally:
            conn.close()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return report(schema_skew.STATUS_UNSTAMPED, None, head, "no alembic_version stamp")
        return report(schema_skew.STATUS_UNKNOWN, None, head, "database unreadable")
    except sqlite3.Error:
        return report(schema_skew.STATUS_UNKNOWN, None, head, "database unreadable")
    if row is None or not row[0]:
        return report(schema_skew.STATUS_UNSTAMPED, None, head, "no alembic_version stamp")
    stamp = str(row[0])
    if head is None:
        return report(schema_skew.STATUS_UNKNOWN, stamp, None, "migration script head unresolvable")
    if stamp == head:
        return report(schema_skew.STATUS_OK, stamp, head, "database stamp matches the script head")
    ancestry = registry_track.head_ancestry("sqlite")
    if ancestry is not None and stamp in ancestry:
        return report(schema_skew.STATUS_BEHIND, stamp, head, "stamp is an ancestor of head")
    return report(schema_skew.STATUS_AHEAD, stamp, head, "stamp is not known to this build")
