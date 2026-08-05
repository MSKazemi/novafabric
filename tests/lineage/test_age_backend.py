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
"""AGELineageStore — behavioural parity with the reference SqliteLineageStore.

Runs openCypher against a real Apache AGE (the ``apache/age`` image) via
testcontainers; skips when Docker/testcontainers is unavailable. The at-scale
promotion benchmark is out of scope here — this proves *correctness* parity.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from novafabric.lineage._types import LineageEdge
from novafabric.lineage.backends.sqlite import SqliteLineageStore


@pytest.fixture(scope="module")
def age_dsn():
    try:
        from testcontainers.postgres import PostgresContainer
    except ImportError:
        pytest.skip("testcontainers not installed — skipping AGE lineage tests")
    try:
        with PostgresContainer(
            # Pinned per deploy/IMAGE_PINS.md — matches the project's Postgres 16
            # baseline (postgres:16-alpine in docker-compose.yml). AGE tags are
            # release_PG<major>_<version>, not semver.
            "apache/age:release_PG16_1.6.0",
            username="postgres",
            password="postgres",
            dbname="postgres",
        ) as container:
            url = container.get_connection_url()
            url = url.replace("postgresql+psycopg2://", "postgresql+psycopg://")
            yield url.replace("postgresql+psycopg://", "postgresql://")
    except Exception as exc:  # pragma: no cover - env-dependent
        pytest.skip(f"Could not start apache/age container (Docker unavailable?): {exc}")


def _run(rid: str) -> dict:
    return {"kind": "run", "run_id": rid}


def _asset(ref: str) -> dict:
    return {"kind": "asset", "asset_ref": ref, "registry": "local"}


def _graph_edges() -> list[LineageEdge]:
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
def age_store(age_dsn):
    from novafabric.lineage.backends.age import AGELineageStore

    store = AGELineageStore(age_dsn)
    # Isolate each test: clear the shared graph (module-scoped container).
    store._cypher("MATCH (n) DETACH DELETE n", {})
    _load(store)
    return store


@pytest.fixture
def sqlite_store(tmp_path: Path):
    store = SqliteLineageStore(db_path=tmp_path / "lineage.db")
    _load(store)
    return store


class TestAgeParity:
    def test_provenance_matches_sqlite(self, age_store, sqlite_store) -> None:
        assert _refs(age_store.provenance("01RUNA", depth=5)) == _refs(
            sqlite_store.provenance("01RUNA", depth=5)
        )

    def test_blast_radius_matches_sqlite(self, age_store, sqlite_store) -> None:
        assert _refs(age_store.blast_radius("01RUNA", max_depth=5)) == _refs(
            sqlite_store.blast_radius("01RUNA", max_depth=5)
        )

    def test_blast_radius_depth_bound(self, age_store, sqlite_store) -> None:
        assert _refs(age_store.blast_radius("01RUNA", max_depth=1)) == _refs(
            sqlite_store.blast_radius("01RUNA", max_depth=1)
        )

    def test_replay_chain_matches_sqlite(self, age_store, sqlite_store) -> None:
        age = [r["ref"] for r in age_store.replay_chain("01RUND")]
        assert age == [r["ref"] for r in sqlite_store.replay_chain("01RUND")]
        assert age == ["01RUNC", "01RUNB", "01RUNA"]


class TestAgeBehaviour:
    def test_unknown_run_returns_empty(self, age_store) -> None:
        assert age_store.provenance("nope", depth=5) == []
        assert age_store.blast_radius("nope", max_depth=5) == []
        assert age_store.replay_chain("nope") == []

    def test_insert_is_idempotent_over_nodes(self, age_store) -> None:
        before = age_store.blast_radius("01RUNA", max_depth=5)
        _load(age_store)
        after = age_store.blast_radius("01RUNA", max_depth=5)
        assert _refs(before) == _refs(after)
