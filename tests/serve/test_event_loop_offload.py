"""B4: heavy sync work runs off the event loop.

A slow stats computation must not stall unrelated requests (before this fix
every handler did sync DB/disk IO directly on the single event loop, stalling
SSE/WS heartbeats behind any slow query).
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Iterator

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

import novafabric.serve.app as serve_app  # noqa: E402
from novafabric.serve.app import create_app  # noqa: E402

VALID_TOKEN = "test-token-1234567890abcdef"
HEADERS = {"host": "127.0.0.1:4321"}
TOKEN_Q = f"token={VALID_TOKEN}"


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    runs = tmp_path / "runs"
    runs.mkdir()
    app = create_app(
        token=VALID_TOKEN, capsule_dir=runs, db_path=tmp_path / "r.db", static_dir=None
    )
    with TestClient(app) as c:
        yield c


class TestEventLoopOffload:
    def test_slow_stats_does_not_stall_health(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """/api/health answers fast while /api/stats blocks in its worker."""
        release = threading.Event()
        entered = threading.Event()

        # _compute_stats is a create_app closure, so block at the cache layer:
        # instances resolve get_or_compute through the class.
        def slow_get_or_compute(self, compute):  # type: ignore[no-untyped-def]
            entered.set()
            release.wait(timeout=10)
            return {"run_count": 0, "asset_count": 0}

        monkeypatch.setattr(serve_app._StatsCache, "get_or_compute", slow_get_or_compute)

        results: dict[str, float] = {}

        def call_stats() -> None:
            client.get(f"/api/stats?{TOKEN_Q}", headers=HEADERS)

        t = threading.Thread(target=call_stats, daemon=True)
        t.start()
        assert entered.wait(timeout=5), "stats computation never started"

        # While stats is blocked in its worker thread, health must still answer.
        start = time.monotonic()
        resp = client.get("/api/health", headers=HEADERS)
        results["health_secs"] = time.monotonic() - start
        release.set()
        t.join(timeout=10)

        assert resp.status_code == 200
        assert results["health_secs"] < 2.0, (
            f"/api/health took {results['health_secs']:.2f}s while /api/stats "
            "was blocked — event loop is stalled by sync work"
        )

    def test_threaded_sqlite_does_not_500(self, client: TestClient) -> None:
        """The runs read path moved into a worker thread; the whole sqlite
        connection lifecycle must live there (thread-bound connections)."""
        for path in ("/api/runs", "/api/runs/search"):
            resp = client.get(f"{path}?{TOKEN_Q}", headers=HEADERS)
            assert resp.status_code == 200, f"{path}: {resp.status_code}"
