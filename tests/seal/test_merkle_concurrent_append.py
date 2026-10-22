"""Concurrent appends to one SQLite Merkle log (audit, unconfirmed item, 2026-10-22).

``MerkleLog.append`` read ``MAX(leaf_index) + 1`` in a deferred transaction and only
then took the write lock with its ``INSERT``. Two ``nova capture`` processes sealing
into one log could read the same index; the second ``INSERT`` failed with
``sqlite3.IntegrityError`` — not even the ``MerkleError`` the seal path catches —
and that capsule was left unsealed with a stderr warning. Measured before the fix:
10 of 200 appends failed with 8 processes x 25 appends.
"""

from __future__ import annotations

import multiprocessing as mp
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from novafabric.trust.novaseal.merkle import MerkleError, MerkleLog

_PROCS = 8
_APPENDS = 25


def _worker(db: str, wid: int, n: int, barrier: Any, out: Any) -> None:
    log = MerkleLog(Path(db))
    barrier.wait()
    failures = 0
    for i in range(n):
        try:
            log.append({"capsule_id": f"{wid:02d}-{i:04d}", "keyid": "k", "has_tsr": False})
        except Exception:  # noqa: BLE001 — counted, the assertion reports it
            failures += 1
    log.close()
    out.put(failures)


def test_concurrent_processes_never_lose_an_append(tmp_path: Path) -> None:
    db = tmp_path / "merkle.db"
    MerkleLog(db).close()
    ctx = mp.get_context("spawn")
    barrier = ctx.Barrier(_PROCS)
    out = ctx.Queue()
    procs = [
        ctx.Process(target=_worker, args=(str(db), w, _APPENDS, barrier, out))
        for w in range(_PROCS)
    ]
    for p in procs:
        p.start()
    failures = sum(out.get(timeout=300) for _ in procs)
    for p in procs:
        p.join(timeout=60)

    assert failures == 0
    log = MerkleLog(db)
    try:
        result = log.verify_consistency()
    finally:
        log.close()
    assert result.leaf_count == _PROCS * _APPENDS
    assert result.consistent, result.errors


def test_concurrent_first_opens_of_a_new_log_all_succeed(tmp_path: Path) -> None:
    """Two captures sealing into a log that does not exist yet: every open and
    every append succeeds (journal mode is set once, tolerating the race)."""
    db = tmp_path / "fresh" / "merkle.db"
    ctx = mp.get_context("spawn")
    barrier = ctx.Barrier(_PROCS)
    out = ctx.Queue()
    procs = [
        ctx.Process(target=_cold_worker, args=(str(db), w, barrier, out)) for w in range(_PROCS)
    ]
    for p in procs:
        p.start()
    failures = [out.get(timeout=300) for _ in procs]
    for p in procs:
        p.join(timeout=60)
    assert [f for f in failures if f] == []
    log = MerkleLog(db)
    try:
        assert log.verify_consistency().leaf_count == _PROCS
    finally:
        log.close()


def _cold_worker(db: str, wid: int, barrier: Any, out: Any) -> None:
    barrier.wait()
    try:
        log = MerkleLog(Path(db))
        log.append({"capsule_id": f"cold-{wid}", "keyid": "k", "has_tsr": False})
        log.close()
    except Exception as exc:  # noqa: BLE001 — reported through the queue
        out.put(f"{type(exc).__name__}: {exc}")
        return
    out.put("")


def test_a_database_error_on_append_is_a_merkle_error(tmp_path: Path) -> None:
    """The seal path catches MerkleError; a raw sqlite3 error escaped it."""
    log = MerkleLog(tmp_path / "merkle.db")
    try:
        log.append({"capsule_id": "a", "keyid": "k", "has_tsr": False})
        log._conn.execute("DROP TABLE leaves")  # noqa: SLF001 — force a database fault
        with pytest.raises(MerkleError):
            log.append({"capsule_id": "b", "keyid": "k", "has_tsr": False})
    finally:
        log.close()
    assert not isinstance(MerkleError("x"), sqlite3.Error)
