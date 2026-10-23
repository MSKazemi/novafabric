"""Two concurrent admin revokes cannot leave zero admins (review r6, ADR-0060).

``revoke_role`` counted the other admins and then deleted, in SQLite's default
deferred transaction: both revokes of the last two admins could read "one other
admin remains" before either deleted, and both deletes then succeeded. No admin row
was left, and without OIDC nobody could manage roles any more.

The check and the delete now run under ``BEGIN IMMEDIATE``, which takes SQLite's
write lock before the count, so the second revoke counts after the first commits.

The test makes the interleaving deterministic the way the review did: each revoke
waits at a barrier just before its DELETE. Before the fix both revokes reach it, the
barrier releases them together, and both delete. After the fix the second revoke is
blocked at ``BEGIN IMMEDIATE`` while the first holds the lock; the first one's wait
times out, it deletes and commits, and the second then sees no other admin.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any

import pytest

from novafabric.server import rbac_store
from novafabric.server.rbac_store import LastAdminError

BARRIER_WAIT_S = 1.0  # well under sqlite3's 5 s default busy timeout


class _PauseBeforeDelete:
    """A connection proxy that waits at *barrier* before each role DELETE."""

    def __init__(self, conn: sqlite3.Connection, barrier: threading.Barrier) -> None:
        self._conn = conn
        self._barrier = barrier

    def execute(self, sql: str, *args: Any) -> sqlite3.Cursor:
        if sql.lstrip().startswith("DELETE FROM role_assignments"):
            try:
                self._barrier.wait(timeout=BARRIER_WAIT_S)
            except threading.BrokenBarrierError:
                pass  # the other revoke never got here: it is waiting for the lock
        return self._conn.execute(sql, *args)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


def _admins(db: Path) -> int:
    conn = sqlite3.connect(db)
    try:
        return int(
            conn.execute("SELECT count(*) FROM role_assignments WHERE role = 'admin'").fetchone()[0]
        )
    finally:
        conn.close()


def test_concurrent_revokes_of_the_last_two_admins_leave_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "rbac.db"
    rbac_store.assign_role("alice", "admin", "seed", db_path=db)
    rbac_store.assign_role("bob", "admin", "seed", db_path=db)
    barrier = threading.Barrier(2)
    real_get_conn = rbac_store._get_conn
    monkeypatch.setattr(
        rbac_store, "_get_conn", lambda p: _PauseBeforeDelete(real_get_conn(p), barrier)
    )
    outcomes: dict[str, object] = {}

    def revoke(subject: str) -> None:
        try:
            outcomes[subject] = rbac_store.revoke_role(
                subject, "admin", db_path=db, oidc_configured=False
            )
        except LastAdminError as exc:
            outcomes[subject] = exc

    threads = [threading.Thread(target=revoke, args=(s,)) for s in ("alice", "bob")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert _admins(db) == 1, f"admins left: {_admins(db)}; outcomes: {outcomes}"
    assert sorted(type(v).__name__ for v in outcomes.values()) == ["LastAdminError", "bool"]
    assert [v for v in outcomes.values() if isinstance(v, bool)] == [True]


def test_a_refused_revoke_leaves_no_transaction_open(tmp_path: Path) -> None:
    db = tmp_path / "rbac.db"
    rbac_store.assign_role("alice", "admin", "seed", db_path=db)

    with pytest.raises(LastAdminError):
        rbac_store.revoke_role("alice", "admin", db_path=db, oidc_configured=False)

    # The write lock was released: another writer gets it at once.
    conn = sqlite3.connect(db, timeout=0.5)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.rollback()
    finally:
        conn.close()
    assert _admins(db) == 1


def test_revoking_a_missing_assignment_still_reports_not_found(tmp_path: Path) -> None:
    db = tmp_path / "rbac.db"
    rbac_store.assign_role("alice", "admin", "seed", db_path=db)
    assert rbac_store.revoke_role("nobody", "admin", db_path=db) is False
    assert rbac_store.revoke_role("alice", "viewer", db_path=db) is False
