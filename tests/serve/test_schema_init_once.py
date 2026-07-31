"""B4: registry schema DDL runs once per (process, db file), not per request.

Before this fix every dashboard read handler ran the full CREATE-TABLE
executescript on its per-request connection.
"""

from __future__ import annotations

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