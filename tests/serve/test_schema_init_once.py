"""B4: registry schema DDL runs once per (process, db file), not per request.

Before this fix every dashboard read handler ran the full CREATE-TABLE
executescript on its per-request connection.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Iterator

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from novafabric.registry import store  # noqa: E402
from novafabric.serve.app import create_app  # noqa: E402

VALID_TOKEN = "test-token-1234567890abcdef"
HEADERS = {"host": "127.0.0.1:4321"}
TOKEN_Q = f"token={VALID_TOKEN}"


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    runs = tmp_path / "runs"
    runs.mkdir()
    store.reset_schema_memo()
    app = create_app(
        token=VALID_TOKEN, capsule_dir=runs, db_path=tmp_path / "r.db", static_dir=None
    )
    with TestClient(app) as c:
        yield c
    store.reset_schema_memo()


class TestSchemaInitOnce:
    def test_ddl_runs_once_across_requests(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = {"n": 0}
        real = store._init_schema

        def counting(conn) -> None:  # type: ignore[no-untyped-def]
            calls["n"] += 1
            real(conn)

        monkeypatch.setattr(store, "_init_schema", counting)
        store.reset_schema_memo()

        for _ in range(3):
            resp = client.get(f"/api/runs?{TOKEN_Q}", headers=HEADERS)
            assert resp.status_code == 200
        assert calls["n"] == 1

    def test_reset_memo_reinitialises(self, tmp_path: Path) -> None:
        db = tmp_path / "memo.db"
        conn = store.get_connection(db)
        store.init_schema(conn)
        conn.close()

        # Simulate the db file being replaced out from under the process.
        db.unlink()
        store.reset_schema_memo()
        conn2 = store.get_connection(db)
        store.init_schema(conn2)
        # Schema exists again — a registry query works.
        assert conn2.execute("SELECT COUNT(*) FROM assets").fetchone()[0] == 0
        conn2.close()

    def test_force_bypasses_memo(self, tmp_path: Path) -> None:
        db = tmp_path / "force.db"
        conn = store.get_connection(db)
        store.init_schema(conn)
        conn.close()

        db.unlink()
        conn2 = store.get_connection(db)
        store.init_schema(conn2, force=True)
        assert conn2.execute("SELECT COUNT(*) FROM assets").fetchone()[0] == 0
        conn2.close()

class TestConcurrentInitIsSerialised:
    """The memo must hold under the concurrency ``serve`` actually creates.

    ``init_schema`` used to check the memo under the lock, *release* it, run the DDL, then
    re-take the lock to record the result. Two threads could both miss and both run the
    full ``executescript``. That is not a hypothetical arrangement: the lifespan's
    ``run_in_executor`` bootstrap, the stats refresh thread, the capsule-watcher thread and
    the request handlers all call this against the same db file.

    The pre-existing ``test_ddl_runs_once_across_requests`` could only catch that by luck,
    because it depends on the two callers actually interleaving. This one removes the luck:
    a barrier releases every thread into the critical section at once, and the patched DDL
    sleeps to hold the window open. Against the check-then-act version this fails with a
    count equal to the number of threads; against the single-hold version it is 1.
    """

    def test_parallel_first_init_runs_the_ddl_exactly_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import threading

        db = tmp_path / "race.db"
        n_threads = 8
        calls: list[int] = []
        calls_lock = threading.Lock()
        real = store._init_schema

        def slow_counting(conn) -> None:  # type: ignore[no-untyped-def]
            with calls_lock:
                calls.append(1)
            time.sleep(0.05)  # hold the window open so a lost race is certain, not likely
            real(conn)

        monkeypatch.setattr(store, "_init_schema", slow_counting)
        store.reset_schema_memo()

        start = threading.Barrier(n_threads)
        errors: list[BaseException] = []

        def worker() -> None:
            try:
                conn = store.get_connection(db)
                start.wait(timeout=10)
                store.init_schema(conn)
                conn.close()
            except BaseException as exc:  # noqa: BLE001 - reported, not swallowed
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors, f"worker raised: {errors!r}"
        assert not any(t.is_alive() for t in threads), "a worker deadlocked"
        assert len(calls) == 1, (
            f"{len(calls)} of {n_threads} threads ran the DDL; the memo is check-then-act"
        )
