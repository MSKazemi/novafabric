"""Postgres writer-lease parity (ADR-0244 slice 1, testcontainers tier).

Runs the identical behavioral contract as the SQLite twin
(``tests/ha/test_lease.py``) so the two implementations cannot drift, plus a
two-connection race — the case Postgres exists for.
"""

from __future__ import annotations

import threading

import pytest

pytest.importorskip("psycopg")

from ha.test_lease import run_lease_contract  # noqa: E402

from novafabric.ha import LeaseState, PostgresLeaseStore  # noqa: E402


def test_lease_contract_postgres(postgres_url: str) -> None:
    run_lease_contract(PostgresLeaseStore(postgres_url))


def test_concurrent_acquisition_across_connections(postgres_url: str) -> None:
    # A separate store instance per contender — separate connections, the
    # real deployment shape.
    winners: list[LeaseState] = []
    lock = threading.Lock()
    start = threading.Barrier(4)

    def contender(i: int) -> None:
        start.wait()
        got = PostgresLeaseStore(postgres_url).acquire(
            f"race-node-{i}", ttl_seconds=30.0
        )
        if got is not None:
            with lock:
                winners.append(got)

    threads = [threading.Thread(target=contender, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(winners) == 1, f"expected one winner, got {len(winners)}"
