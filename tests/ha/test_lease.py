"""Writer-lease contract (ADR-0244 slice 1) against the SQLite twin.

The Postgres implementation runs the same contract in
``tests/metadata_store/test_writer_lease_postgres.py`` (testcontainers tier),
so the two backends cannot drift behaviorally.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from novafabric.ha import LeaseState, SqliteLeaseStore, WriterLeaseStore


@pytest.fixture()
def store(tmp_path: Path) -> SqliteLeaseStore:
    return SqliteLeaseStore(tmp_path / "lease.db")


def run_lease_contract(store: WriterLeaseStore) -> None:
    """The shared behavioral contract — imported by the Postgres tier too."""
    # Fresh store: nobody holds the lease.
    assert store.current() is None

    # First acquisition wins and mints token 1.
    lease = store.acquire("writer-a", ttl_seconds=30.0)
    assert isinstance(lease, LeaseState)
    assert lease.holder_id == "writer-a"
    assert lease.fencing_token == 1

    # A second node cannot take an unexpired lease.
    assert store.acquire("writer-b", ttl_seconds=30.0) is None

    # Re-acquisition by the holder extends without bumping the token —
    # the writer's identity did not change, so no fence moves.
    again = store.acquire("writer-a", ttl_seconds=30.0)
    assert again is not None and again.fencing_token == 1

    # Renew is holder-guarded.
    assert store.renew("writer-a", ttl_seconds=30.0) is True
    assert store.renew("writer-b", ttl_seconds=30.0) is False

    # Expiry: the lease lapses, takeover succeeds and BUMPS the token —
    # the deposed writer now holds a strictly lower fence.
    assert store.acquire("writer-a", ttl_seconds=-1.0) is not None  # force-expire
    taken = store.acquire("writer-b", ttl_seconds=30.0)
    assert taken is not None
    assert taken.holder_id == "writer-b"
    assert taken.fencing_token == 2

    # The deposed writer can no longer renew — it must halt mutating work.
    assert store.renew("writer-a", ttl_seconds=30.0) is False

    # Clean demotion: release is holder-guarded and reopens the lease.
    assert store.release("writer-a") is False
    assert store.release("writer-b") is True
    reacquired = store.acquire("writer-a", ttl_seconds=30.0)
    assert reacquired is not None and reacquired.fencing_token == 3

    # Leave the lease unheld — the contract must not leak state into
    # whatever shares the store (the Postgres tier reuses one container).
    assert store.release("writer-a") is True


def test_lease_contract_sqlite(store: SqliteLeaseStore) -> None:
    run_lease_contract(store)


def test_expired_check_uses_caller_time(store: SqliteLeaseStore) -> None:
    lease = store.acquire("w", ttl_seconds=30.0)
    assert lease is not None
    assert lease.expired(now=time.time() + 60) is True
    assert lease.expired(now=time.time()) is False


def test_concurrent_acquisition_has_one_winner(tmp_path: Path) -> None:
    """N threads race for a fresh lease: exactly one acquires, and the token
    is minted exactly once."""
    store = SqliteLeaseStore(tmp_path / "race.db")
    winners: list[LeaseState] = []
    lock = threading.Lock()
    start = threading.Barrier(8)

    def contender(i: int) -> None:
        start.wait()
        got = store.acquire(f"node-{i}", ttl_seconds=30.0)
        if got is not None:
            with lock:
                winners.append(got)

    threads = [threading.Thread(target=contender, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(winners) == 1, f"expected one winner, got {len(winners)}"
    assert winners[0].fencing_token == 1
    current = store.current()
    assert current is not None and current.holder_id == winners[0].holder_id
