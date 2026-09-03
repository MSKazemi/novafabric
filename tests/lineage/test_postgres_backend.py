# Copyright 2024 NovaFabric Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""PostgresLineageStore — behavioural parity with the reference SqliteLineageStore.

The Postgres backend must give byte-for-byte the same query answers as the
SQLite reference on the same graph (ADR-0053 Phase 6). This tier uses a real
Postgres via testcontainers; it skips when Docker/testcontainers is unavailable.
The 10M-edge p99<500ms promotion benchmark is a separate, infra-heavy gate and
is NOT asserted here — this proves *correctness*, not scale.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lineage import contract
from novafabric.lineage._types import LineageEdge
from novafabric.lineage.backends.sqlite import SqliteLineageStore


@pytest.fixture(scope="module")
def postgres_dsn():
    try:
        from testcontainers.postgres import PostgresContainer
    except ImportError:
        pytest.skip("testcontainers not installed — skipping Postgres lineage tests")
    try:
        with PostgresContainer("postgres:16-alpine") as container:
            url = container.get_connection_url()
            url = url.replace("postgresql+psycopg2://", "postgresql+psycopg://")
            yield url.replace("postgresql+psycopg://", "postgresql://")
    except Exception as exc:  # pragma: no cover - env-dependent
        pytest.skip(f"Could not start Postgres container (Docker unavailable?): {exc}")


def _run(rid: str) -> dict:
    return {"kind": "run", "run_id": rid}


def _asset(ref: str) -> dict:
    return {"kind": "asset", "asset_ref": ref, "registry": "local"}


def _graph_edges() -> list[LineageEdge]:
    """A small graph: a replay chain D->C->B->A plus a run->asset production."""
    return [
        LineageEdge(edge_type="consumed", source=_run("01RUNA"),
                    target=_asset("model:foo@1.0.0"), confidence="high",
                    capsule_run_id="01RUNA"),
        LineageEdge(edge_type="replayed_from", source=_run("01RUNB"),
                    target=_run("01RUNA"), confidence="high", capsule_run_id="01RUNB"),
        LineageEdge(edge_type="replayed_from", source=_run("01RUNC"),
                    target=_run("01RUNB"), confidence="high", capsule_run_id="01RUNC"),
        LineageEdge(edge_type="replayed_from", source=_run("01RUND"),
                    target=_run("01RUNC"), confidence="high", capsule_run_id="01RUND"),
    ]


def _refs(rows: list[dict]) -> set[str]:
    return {r["ref"] for r in rows}


def _load(store) -> None:
    for edge in _graph_edges():
        store.insert(edge)


@pytest.fixture
def pg_store(postgres_dsn):
    from novafabric.lineage.backends.postgres import PostgresLineageStore

    store = PostgresLineageStore(postgres_dsn)
    # Isolate each test: the module-scoped container shares one DB, and
    # LineageEdge.edge_id is a fresh ULID per construction, so without a reset
    # re-`_load` across tests would pile up duplicate-payload edges (parity with
    # the SQLite fixture, which gets a fresh tmp_path DB each test).
    store._conn.execute("TRUNCATE lineage_edges, lineage_nodes")
    _load(store)
    return store


@pytest.fixture
def sqlite_store(tmp_path: Path):
    store = SqliteLineageStore(db_path=tmp_path / "lineage.db")
    _load(store)
    return store


class TestPostgresParity:
    def test_provenance_matches_sqlite(self, pg_store, sqlite_store) -> None:
        assert _refs(pg_store.provenance("01RUNA", depth=5)) == _refs(
            sqlite_store.provenance("01RUNA", depth=5)
        )

    def test_blast_radius_matches_sqlite(self, pg_store, sqlite_store) -> None:
        assert _refs(pg_store.blast_radius("01RUNA", max_depth=5)) == _refs(
            sqlite_store.blast_radius("01RUNA", max_depth=5)
        )

    def test_replay_chain_matches_sqlite(self, pg_store, sqlite_store) -> None:
        pg = pg_store.replay_chain("01RUND")
        sq = sqlite_store.replay_chain("01RUND")
        # same nodes, same order (replay_chain is step-ordered)
        assert [r["ref"] for r in pg] == [r["ref"] for r in sq]
        assert [r["ref"] for r in pg] == ["01RUNC", "01RUNB", "01RUNA"]

    def test_blast_radius_depth_bound(self, pg_store, sqlite_store) -> None:
        assert _refs(pg_store.blast_radius("01RUNA", max_depth=1)) == _refs(
            sqlite_store.blast_radius("01RUNA", max_depth=1)
        )


class TestPostgresBehaviour:
    def test_unknown_run_returns_empty(self, pg_store) -> None:
        assert pg_store.provenance("nope", depth=5) == []
        assert pg_store.blast_radius("nope", max_depth=5) == []
        assert pg_store.replay_chain("nope") == []

    def test_insert_is_idempotent(self, pg_store) -> None:
        # Re-inserting the same edges must not duplicate nodes/edges.
        before = pg_store.blast_radius("01RUNA", max_depth=5)
        _load(pg_store)
        after = pg_store.blast_radius("01RUNA", max_depth=5)
        assert _refs(before) == _refs(after)


# ---------------------------------------------------------------------------
# The shared backend contract
# ---------------------------------------------------------------------------
# The parity classes above compare Postgres to SQLite. That is a good check but
# an incomplete one: a differential assertion cannot say which side is wrong when
# the two disagree, and it cannot run at all on a machine with one backend. The
# contract states the expected answers absolutely, and is the same contract the
# embedded backends run on a laptop with no container.


class TestPostgresLineageContract:
    @pytest.mark.parametrize("check", contract.contract_params())
    def test_contract(self, check: str, pg_store) -> None:
        contract.CONTRACT_CHECKS[check](pg_store)
