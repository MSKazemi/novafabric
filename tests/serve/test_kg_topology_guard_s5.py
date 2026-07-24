"""S5 scale slice — KG topology graph guards (ADR-0199).

``GET /api/kg/topology`` previously let a caller pass any ``max_nodes`` (so the
node cap could be defeated) and returned edges that could dangle past the node
cap. This suite pins the guard: ``max_nodes``/``max_edges`` are bounded
server-side (out-of-range → 422), and ``get_topology_graph`` drops edges whose
endpoints fell outside the node cap while reporting ``truncated`` +
``truncated_reason``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

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


class TestEndpointBounds:
    def test_max_nodes_over_cap_rejected(self, client: TestClient) -> None:
        r = client.get(f"/api/kg/topology?max_nodes=99999&{TOKEN_Q}", headers=HEADERS)
        assert r.status_code == 422

    def test_max_nodes_zero_rejected(self, client: TestClient) -> None:
        r = client.get(f"/api/kg/topology?max_nodes=0&{TOKEN_Q}", headers=HEADERS)
        assert r.status_code == 422

    def test_max_edges_over_cap_rejected(self, client: TestClient) -> None:
        r = client.get(f"/api/kg/topology?max_edges=999999&{TOKEN_Q}", headers=HEADERS)
        assert r.status_code == 422

    def test_in_range_accepted(self, client: TestClient) -> None:
        # KG may be uninitialised (ok:false) — the point is the request validates.
        r = client.get(f"/api/kg/topology?max_nodes=10&max_edges=10&{TOKEN_Q}", headers=HEADERS)
        assert r.status_code == 200


class TestStoreDanglingEdgeGuard:
    def test_node_cap_drops_dangling_edges_and_flags_truncated(self, tmp_path: Path) -> None:
        pytest.importorskip("kuzu")
        from novafabric.kg.pipeline import KGIngestionPipeline
        from novafabric.kg.store import KGStore

        store = KGStore(tmp_path / "t.kuzu")
        store.init_schema()
        pipe = KGIngestionPipeline(store)
        # Several agents each calling a model → many nodes + edges.
        for i in range(6):
            pipe.ingest_event({
                "event_type": "ModelCallCompleted",
                "agent_id": f"agent-{i}",
                "model_id": f"model-{i}",
                "capsule_id": "c1",
            })
        pipe.flush_to_store()

        # Cap nodes hard so most CALLS edges reference capped-out nodes.
        graph = store.get_topology_graph(max_nodes=2)
        assert len(graph["nodes"]) == 2
        # Every returned edge's endpoints must be in the returned node set.
        node_ids = {n["id"] for n in graph["nodes"]}
        for e in graph["edges"]:
            assert e["src"] in node_ids and e["dst"] in node_ids
        assert graph["truncated"] is True
        assert graph["truncated_reason"]

    def test_full_graph_not_truncated(self, tmp_path: Path) -> None:
        pytest.importorskip("kuzu")
        from novafabric.kg.pipeline import KGIngestionPipeline
        from novafabric.kg.store import KGStore

        store = KGStore(tmp_path / "t2.kuzu")
        store.init_schema()
        pipe = KGIngestionPipeline(store)
        pipe.ingest_event({
            "event_type": "ModelCallCompleted",
            "agent_id": "a", "model_id": "m", "capsule_id": "c1",
        })
        pipe.flush_to_store()
        graph = store.get_topology_graph()
        assert graph["truncated"] is False
        assert graph["truncated_reason"] == []
