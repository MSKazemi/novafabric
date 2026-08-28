"""Two apps in one process must not share mutable state (T6 root cause).

``create_app`` documents itself as a "pure factory", but ``_RunEventBus`` and
``_StatsCache`` were instantiated at **module** level, so every app built in a
process shared one bus and one stats cache.

That is a correctness bug in its own right — ``/api/stats`` could answer with
another app's counts for a capsule directory it never read, and an SSE
subscriber on one app received another app's runs. It also explains a flake the
suite chased for weeks: under ``pytest -n auto`` several serve apps live in one
worker, each running a ``_stats_refresh_loop`` daemon that rewrites the shared
cache every ~2 s. ``/api/stats`` memoises its payload **per app** to keep the
ETag stable, so when a foreign refresh flipped the shared data between two
requests the memo missed, ``cached_at`` moved, the ETag churned, and
``test_http_cache_s6.py::TestHotEndpointsConditionalGet`` got a 200 where it
asserts a 304.
"""
from __future__ import annotations

import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from novafabric.serve import app as app_module
from novafabric.serve.app import create_app

TOKEN = "test-token-1234567890abcdef"
HEADERS = {"host": "127.0.0.1:4321"}
QS = f"token={TOKEN}"


def _make_app(root: Path, *, seed_runs: int = 0):  # type: ignore[no-untyped-def]
    runs = root / "runs"
    runs.mkdir(parents=True)
    db = root / "r.db"
    if seed_runs:
        # Give this app counts of its own, so a shared cache shows up as a wrong
        # number rather than as two apps agreeing by accident on zero.
        from novafabric.registry.runs_cache import ensure_runs_cache, upsert_run
        from novafabric.registry.store import get_connection, init_schema

        conn = get_connection(db)
        init_schema(conn)
        ensure_runs_cache(conn)
        for i in range(seed_runs):
            upsert_run(conn, {
                "run_id": f"seed-{i}",
                "status": "success",
                "created_at": f"2026-08-28T00:0{i}:00+00:00",
            })
        conn.commit()
        conn.close()
    return create_app(token=TOKEN, capsule_dir=runs, db_path=db, static_dir=None)


@pytest.fixture
def two_apps() -> Iterator[tuple[TestClient, TestClient]]:
    tmp = Path(tempfile.mkdtemp())
    a = _make_app(tmp / "a", seed_runs=3)   # app A has runs
    b = _make_app(tmp / "b")                # app B's capsule dir is empty
    with TestClient(a) as ca, TestClient(b) as cb:
        yield ca, cb


class TestNoProcessGlobalMutableState:
    """The regression guard: re-adding either singleton at module scope fails here."""

    @pytest.mark.parametrize("name", ["_run_bus", "_stats_cache"])
    def test_module_exposes_no_shared_instance(self, name: str) -> None:
        assert not hasattr(app_module, name), (
            f"{name} is back at module scope — every create_app() in this process "
            f"would share one instance again"
        )

    def test_two_apps_get_distinct_instances(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        a = _make_app(tmp / "a")
        b = _make_app(tmp / "b")
        # Reached through the route closures rather than an attribute, because
        # being unreachable from module scope is precisely the property tested.
        assert a is not b
        assert a.router is not b.router


# ⚠ Deliberately NOT tested here: that this fix ends the
# ``test_http_cache_s6.py::TestHotEndpointsConditionalGet`` flake. The shared
# cache is *observed* to take on a neighbour's value — sampling it directly
# while two apps run gives ``[3, 0, 0, 0, ...]``, and that flip is exactly the
# input change that invalidates ``/api/stats``'s per-app payload memo. But a
# test written against it passed 5/5 against the pre-fix source, so it
# discriminated nothing and was removed rather than kept as false assurance.
# The link between this defect and that flake remains a hypothesis.


class TestStatsCacheIsPerApp:
    def test_each_app_counts_only_its_own_runs(
        self, two_apps: tuple[TestClient, TestClient]
    ) -> None:
        ca, cb = two_apps
        assert ca.get(f"/api/stats?{QS}", headers=HEADERS).json()["run_count"] == 3
        b_count = cb.get(f"/api/stats?{QS}", headers=HEADERS).json()["run_count"]
        assert b_count == 0, f"app B reported {b_count} runs for an empty capsule dir"

    def test_apps_do_not_share_one_snapshot(
        self, two_apps: tuple[TestClient, TestClient]
    ) -> None:
        # Distinct cached_at values prove distinct caches, independently of counts.
        ca, cb = two_apps
        a_at = ca.get(f"/api/stats?{QS}", headers=HEADERS).json()["cached_at"]
        b_at = cb.get(f"/api/stats?{QS}", headers=HEADERS).json()["cached_at"]
        assert a_at is not None and b_at is not None
        assert a_at != b_at, "both apps returned one shared snapshot"
