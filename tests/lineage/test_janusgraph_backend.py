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
"""JanusGraphLineageStore — behavioural parity with SqliteLineageStore.

Runs Gremlin against a real JanusGraph server (the ``janusgraph/janusgraph``
image) via testcontainers; skips when Docker/testcontainers/gremlinpython are
unavailable. The JanusGraph backend is **run-centric** (it models run vertices
only), so parity is asserted on a run-only graph. This suite also pins the two
correctness fixes the first live verification surfaced: the GraphSON serializer
(GraphBinary cannot decode JanusGraph's custom vertex ids) and the `.emit()` in
provenance/blast_radius (without it only depth-exact nodes are returned).
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from novafabric.lineage._types import LineageEdge
from novafabric.lineage.backends.sqlite import SqliteLineageStore


@pytest.fixture(scope="module")
def janusgraph_endpoint():
    try:
        import gremlin_python  # noqa: F401
        from testcontainers.core.container import DockerContainer
        from testcontainers.core.waiting_utils import wait_for_logs
    except ImportError:
        pytest.skip("gremlinpython/testcontainers not installed — skipping JanusGraph tests")
    # Pinned per deploy/IMAGE_PINS.md — keep in sync with the compose/Helm pin.
    container = DockerContainer("janusgraph/janusgraph:1.1.0").with_exposed_ports(8182)
    try:
        container.start()
        wait_for_logs(container, "Channel started at port 8182", timeout=150)
        time.sleep(3)  # server accepts the port before the graph is fully ready
        host = container.get_container_host_ip()
        port = container.get_exposed_port(8182)
        yield f"ws://{host}:{port}/gremlin"
    except Exception as exc:  # pragma: no cover - env-dependent
        pytest.skip(f"Could not start JanusGraph container (Docker unavailable?): {exc}")
    finally:
        try:
            container.stop()
        except Exception:  # pragma: no cover
            pass


def _run(rid: str) -> dict:
    return {"kind": "run", "run_id": rid}


def _run_edges() -> list[LineageEdge]:
    """A run-only replay chain D->C->B->A (JanusGraph models run vertices only)."""
    return [
        LineageEdge(edge_type="replayed_from", source=_run("01RUNB"),
                    target=_run("01RUNA"), confidence="high", capsule_run_id="01RUNB"),
        LineageEdge(edge_type="replayed_from", source=_run("01RUNC"),
                    target=_run("01RUNB"), confidence="high", capsule_run_id="01RUNC"),
        LineageEdge(edge_type="replayed_from", source=_run("01RUND"),
                    target=_run("01RUNC"), confidence="high", capsule_run_id="01RUND"),
    ]


def _refs(rows: list[dict]) -> set[str]:
    return {r["ref"] for r in rows}


@pytest.fixture
def jg_store(janusgraph_endpoint):
    from novafabric.lineage.backends.janusgraph import JanusGraphLineageStore

    store = JanusGraphLineageStore(gremlin_endpoint=janusgraph_endpoint)
    store._connect()
    # `.toList()` not `.iterate()`: this gremlinpython emits a `discard()` step
    # for iterate() that this JanusGraph server version rejects (599). toList()
    # forces the drop to execute and is version-safe.
    store._g.V().drop().toList()  # isolate each test on the shared server
    for edge in _run_edges():
        store.insert(edge)
    yield store
    store.close()


@pytest.fixture
def sqlite_store(tmp_path: Path):
    store = SqliteLineageStore(db_path=tmp_path / "lineage.db")
    for edge in _run_edges():
        store.insert(edge)
    return store


class TestJanusGraphParity:
    def test_provenance_matches_sqlite(self, jg_store, sqlite_store) -> None:
        assert _refs(jg_store.provenance("01RUND", depth=5)) == _refs(
            sqlite_store.provenance("01RUND", depth=5)
        )

    def test_blast_radius_matches_sqlite(self, jg_store, sqlite_store) -> None:
        assert _refs(jg_store.blast_radius("01RUNA", max_depth=5)) == _refs(
            sqlite_store.blast_radius("01RUNA", max_depth=5)
        )

    def test_replay_chain_matches_sqlite(self, jg_store, sqlite_store) -> None:
        assert _refs(jg_store.replay_chain("01RUND")) == _refs(
            sqlite_store.replay_chain("01RUND")
        )

    def test_provenance_is_nonempty(self, jg_store) -> None:
        # Regression pin: the pre-fix code (no .emit()) returned [] here.
        assert _refs(jg_store.provenance("01RUND", depth=5)) == {"01RUNC", "01RUNB", "01RUNA"}
