"""JanusGraph lineage backend tests.

Integration tests gate: NOVA_INTEGRATION=1 + JanusGraph container at
ws://localhost:8182/gremlin.

Unit tests use ``unittest.mock`` to mock the Gremlin traversal source and
connection objects — no live server required.

# 4-hour smoke: hardware-gated, requires production JanusGraph cluster.
# Run manually with: NOVA_INTEGRATION=1 NOVA_SMOKE=1 pytest tests/lineage/test_janusgraph.py -k smoke
"""

from __future__ import annotations

import os
import sys
import types
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from novafabric.lineage._types import LineageEdge
from novafabric.lineage.backends.janusgraph import JanusGraphLineageStore
from novafabric.lineage.backends.janusgraph_snb import LineageSnbQueries
from novafabric.lineage.store import AbstractLineageStore

_INTEGRATION = os.environ.get("NOVA_INTEGRATION") == "1"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_edge(
    src: str = "run-A",
    tgt: str = "run-B",
    edge_type: str = "spawned",
) -> LineageEdge:
    return LineageEdge(
        edge_type=edge_type,
        source={"run_id": src},
        target={"run_id": tgt},
        confidence="high",
        capsule_run_id="cap-001",
    )


def _make_store_with_mock_g() -> tuple[JanusGraphLineageStore, MagicMock]:
    """Return a JanusGraphLineageStore with a mocked Gremlin traversal source."""
    # Patch gremlin_python so __init__ succeeds without the real package.
    gremlin_mod = types.ModuleType("gremlin_python")
    gremlin_mod.__version__ = "3.7.0"  # type: ignore[attr-defined]
    with patch.dict(sys.modules, {"gremlin_python": gremlin_mod}):
        store = JanusGraphLineageStore.__new__(JanusGraphLineageStore)
        store._endpoint = "ws://localhost:8182/gremlin"
        store._site_id = "local"
        store._conn = None
        mock_g = MagicMock()
        store._g = mock_g
        return store, mock_g


# ---------------------------------------------------------------------------
# Unit tests — no live server required
# ---------------------------------------------------------------------------


class TestInit:
    def test_is_subclass_of_abstract(self) -> None:
        """JanusGraphLineageStore must be a subclass of AbstractLineageStore."""
        assert issubclass(JanusGraphLineageStore, AbstractLineageStore)

    def test_methods_exist(self) -> None:
        """JanusGraphLineageStore must define all four ABC methods."""
        assert callable(getattr(JanusGraphLineageStore, "insert", None))
        assert callable(getattr(JanusGraphLineageStore, "provenance", None))
        assert callable(getattr(JanusGraphLineageStore, "blast_radius", None))
        assert callable(getattr(JanusGraphLineageStore, "replay_chain", None))

    def test_init_raises_import_error_when_gremlinpython_missing(self) -> None:
        """Constructing the store without gremlinpython must raise ImportError with install hint."""
        # Force gremlin_python to appear absent.
        with patch.dict(sys.modules, {"gremlin_python": None}):  # type: ignore[dict-item]
            with pytest.raises(ImportError, match="gremlinpython not installed"):
                JanusGraphLineageStore()

    def test_init_stores_endpoint_and_site_id(self) -> None:
        """__init__ must store endpoint and site_id without connecting."""
        gremlin_mod = types.ModuleType("gremlin_python")
        with patch.dict(sys.modules, {"gremlin_python": gremlin_mod}):
            store = JanusGraphLineageStore(
                gremlin_endpoint="ws://custom:9999/gremlin",
                site_id="cluster-1",
            )
        assert store._endpoint == "ws://custom:9999/gremlin"
        assert store._site_id == "cluster-1"
        assert store._g is None
        assert store._conn is None

    def test_init_does_not_connect_eagerly(self) -> None:
        """__init__ must NOT open a WebSocket — connection is lazy."""
        gremlin_mod = types.ModuleType("gremlin_python")
        with patch.dict(sys.modules, {"gremlin_python": gremlin_mod}):
            store = JanusGraphLineageStore()
        # _g and _conn remain None until first operation.
        assert store._g is None
        assert store._conn is None


class TestInsert:
    def test_insert_calls_upsert_and_addE(self) -> None:
        """insert() must call the Gremlin vertex-upsert coalesce idiom and addE."""
        store, mock_g = _make_store_with_mock_g()
        edge = _make_edge()

        # Patch _connect to be a no-op (already connected via mock_g).
        store._connect = MagicMock()  # type: ignore[method-assign]

        # Patch the anonymous traversal import.
        anon_mock = MagicMock()
        with patch.dict(
            sys.modules,
            {"gremlin_python.process.graph_traversal": MagicMock(__=anon_mock)},
        ):
            store.insert(edge)

        # Verify that .V() was called (vertex lookups for upsert + addE).
        assert mock_g.V.called

    def test_insert_uses_edge_type_as_label(self) -> None:
        """insert() must use edge.edge_type as the Gremlin edge label."""
        store, mock_g = _make_store_with_mock_g()
        edge = _make_edge(edge_type="delegated_to")
        store._connect = MagicMock()  # type: ignore[method-assign]

        anon_mock = MagicMock()
        # Capture the addE call label.
        add_e_calls: list[Any] = []
        original_add_e = mock_g.V.return_value.has.return_value.addE

        def capture_add_e(label: str) -> Any:
            add_e_calls.append(label)
            return original_add_e.return_value

        mock_g.V.return_value.has.return_value.addE = capture_add_e  # type: ignore[method-assign]

        with patch.dict(
            sys.modules,
            {"gremlin_python.process.graph_traversal": MagicMock(__=anon_mock)},
        ):
            store.insert(edge)

        assert "delegated_to" in add_e_calls


class TestProvenance:
    def test_provenance_maps_results_to_node_rows(self) -> None:
        """provenance() must convert Gremlin project() results to NodeRow dicts."""
        store, mock_g = _make_store_with_mock_g()
        store._connect = MagicMock()  # type: ignore[method-assign]

        fake_row = {"node_id": "run-B", "kind": "run", "ref": "run-B"}
        mock_g.V.return_value.has.return_value.repeat.return_value.times.return_value.dedup.return_value.project.return_value.by.return_value.by.return_value.by.return_value.toList.return_value = [
            fake_row
        ]

        anon_mock = MagicMock()
        with patch.dict(
            sys.modules,
            {"gremlin_python.process.graph_traversal": MagicMock(__=anon_mock)},
        ):
            rows = store.provenance("run-A", depth=2)

        assert isinstance(rows, list)
        assert rows[0]["node_id"] == "run-B"

    def test_provenance_returns_empty_list_on_no_results(self) -> None:
        """provenance() must return [] when Gremlin returns no vertices."""
        store, mock_g = _make_store_with_mock_g()
        store._connect = MagicMock()  # type: ignore[method-assign]

        # Chain the entire fluent mock to return [].
        (
            mock_g.V.return_value.has.return_value.repeat.return_value.times.return_value.dedup.return_value.project.return_value.by.return_value.by.return_value.by.return_value.toList.return_value
        ) = []

        anon_mock = MagicMock()
        with patch.dict(
            sys.modules,
            {"gremlin_python.process.graph_traversal": MagicMock(__=anon_mock)},
        ):
            rows = store.provenance("run-X", depth=1)

        assert rows == []


class TestBlastRadius:
    def test_blast_radius_traverses_in_edges(self) -> None:
        """blast_radius() must call .in_() on the traversal (upstream direction)."""
        store, mock_g = _make_store_with_mock_g()
        store._connect = MagicMock()  # type: ignore[method-assign]

        anon_mock = MagicMock()
        with patch.dict(
            sys.modules,
            {"gremlin_python.process.graph_traversal": MagicMock(__=anon_mock)},
        ):
            store.blast_radius("run-B", max_depth=3)

        # AnonymousT.in_() should have been called to express upstream traversal.
        assert anon_mock.in_.called

    def test_blast_radius_returns_list(self) -> None:
        """blast_radius() must always return a list."""
        store, mock_g = _make_store_with_mock_g()
        store._connect = MagicMock()  # type: ignore[method-assign]

        (
            mock_g.V.return_value.has.return_value.repeat.return_value.times.return_value.dedup.return_value.project.return_value.by.return_value.by.return_value.by.return_value.toList.return_value
        ) = []

        anon_mock = MagicMock()
        with patch.dict(
            sys.modules,
            {"gremlin_python.process.graph_traversal": MagicMock(__=anon_mock)},
        ):
            result = store.blast_radius("run-Z", max_depth=2)

        assert isinstance(result, list)


class TestReplayChain:
    def test_replay_chain_follows_replayed_from_label(self) -> None:
        """replay_chain() must pass 'replayed_from' as the edge label to out()."""
        store, mock_g = _make_store_with_mock_g()
        store._connect = MagicMock()  # type: ignore[method-assign]

        anon_mock = MagicMock()
        with patch.dict(
            sys.modules,
            {"gremlin_python.process.graph_traversal": MagicMock(__=anon_mock)},
        ):
            store.replay_chain("run-A")

        # AnonymousT.out("replayed_from") should have been called.
        anon_mock.out.assert_called_with("replayed_from")

    def test_replay_chain_returns_list(self) -> None:
        """replay_chain() must always return a list."""
        store, mock_g = _make_store_with_mock_g()
        store._connect = MagicMock()  # type: ignore[method-assign]

        (
            mock_g.V.return_value.has.return_value.repeat.return_value.emit.return_value.project.return_value.by.return_value.by.return_value.by.return_value.toList.return_value
        ) = []

        anon_mock = MagicMock()
        with patch.dict(
            sys.modules,
            {"gremlin_python.process.graph_traversal": MagicMock(__=anon_mock)},
        ):
            result = store.replay_chain("run-A")

        assert isinstance(result, list)


class TestClose:
    def test_close_clears_connection(self) -> None:
        """close() must set _g and _conn to None."""
        store, mock_g = _make_store_with_mock_g()
        mock_conn = MagicMock()
        store._conn = mock_conn

        store.close()

        mock_conn.close.assert_called_once()
        assert store._g is None
        assert store._conn is None

    def test_close_is_idempotent(self) -> None:
        """close() on an unconnected store must not raise."""
        gremlin_mod = types.ModuleType("gremlin_python")
        with patch.dict(sys.modules, {"gremlin_python": gremlin_mod}):
            store = JanusGraphLineageStore()
        store.close()  # should be a no-op, not raise


# ---------------------------------------------------------------------------
# SNB query tests
# ---------------------------------------------------------------------------


class TestLineageSnbQueries:
    def _make_snb(self) -> tuple[LineageSnbQueries, MagicMock]:
        store, mock_g = _make_store_with_mock_g()
        store._connect = MagicMock()  # type: ignore[method-assign]
        q = LineageSnbQueries(store)
        return q, mock_g

    def test_bi_q1_exists_and_callable(self) -> None:
        """LineageSnbQueries.bi_q1_run_frequency must exist and be callable."""
        assert callable(getattr(LineageSnbQueries, "bi_q1_run_frequency", None))

    def test_bi_q3_exists_and_callable(self) -> None:
        """LineageSnbQueries.bi_q3_shortest_path must exist and be callable."""
        assert callable(getattr(LineageSnbQueries, "bi_q3_shortest_path", None))

    def test_bi_q2_exists_and_callable(self) -> None:
        """LineageSnbQueries.bi_q2_top_influential must exist and be callable."""
        assert callable(getattr(LineageSnbQueries, "bi_q2_top_influential", None))

    def test_bi_q4_exists_and_callable(self) -> None:
        """LineageSnbQueries.bi_q4_dependency_chains must exist and be callable."""
        assert callable(getattr(LineageSnbQueries, "bi_q4_dependency_chains", None))

    def test_bi_q5_exists_and_callable(self) -> None:
        """LineageSnbQueries.bi_q5_tenant_activity must exist and be callable."""
        assert callable(getattr(LineageSnbQueries, "bi_q5_tenant_activity", None))

    def test_bi_q1_returns_list(self) -> None:
        """bi_q1_run_frequency() must return a list."""
        q, mock_g = self._make_snb()
        anon_mock = MagicMock()
        # bi_q1 expects one Map from groupCount; simulate empty result.
        mock_g.V.return_value.hasLabel.return_value.has.return_value.groupCount.return_value.by.return_value.order.return_value.by.return_value.toList.return_value = (
            []
        )
        with patch.dict(
            sys.modules,
            {"gremlin_python.process.graph_traversal": MagicMock(__=anon_mock)},
        ):
            result = q.bi_q1_run_frequency("2026-01-01T00:00:00Z")
        assert isinstance(result, list)

    def test_bi_q2_returns_list(self) -> None:
        """bi_q2_top_influential() must return a list."""
        q, mock_g = self._make_snb()
        anon_mock = MagicMock()
        mock_g.V.return_value.hasLabel.return_value.project.return_value.by.return_value.by.return_value.order.return_value.by.return_value.limit.return_value.toList.return_value = (
            []
        )
        with patch.dict(
            sys.modules,
            {"gremlin_python.process.graph_traversal": MagicMock(__=anon_mock)},
        ):
            result = q.bi_q2_top_influential(top_n=5)
        assert isinstance(result, list)

    def test_bi_q3_returns_list(self) -> None:
        """bi_q3_shortest_path() must return a list of run_ids."""
        q, mock_g = self._make_snb()
        anon_mock = MagicMock()
        mock_g.V.return_value.has.return_value.repeat.return_value.until.return_value.path.return_value.by.return_value.limit.return_value.toList.return_value = (
            []
        )
        with patch.dict(
            sys.modules,
            {"gremlin_python.process.graph_traversal": MagicMock(__=anon_mock)},
        ):
            result = q.bi_q3_shortest_path("run-A", "run-Z")
        assert isinstance(result, list)

    def test_bi_q4_returns_list_of_lists(self) -> None:
        """bi_q4_dependency_chains() must return a list of chains (list of lists)."""
        q, mock_g = self._make_snb()
        anon_mock = MagicMock()
        mock_g.V.return_value.has.return_value.repeat.return_value.emit.return_value.path.return_value.by.return_value.toList.return_value = (
            []
        )
        with patch.dict(
            sys.modules,
            {"gremlin_python.process.graph_traversal": MagicMock(__=anon_mock)},
        ):
            result = q.bi_q4_dependency_chains("run-A")
        assert isinstance(result, list)

    def test_bi_q5_returns_dict_with_run_count(self) -> None:
        """bi_q5_tenant_activity() must return a dict with run_count key."""
        q, mock_g = self._make_snb()
        anon_mock = MagicMock()
        # next() returns the run count integer.
        mock_g.V.return_value.hasLabel.return_value.has.return_value.count.return_value.next.return_value = (
            42
        )
        mock_g.V.return_value.hasLabel.return_value.has.return_value.outE.return_value.groupCount.return_value.by.return_value.toList.return_value = (
            [{"spawned": 10}]
        )
        with patch.dict(
            sys.modules,
            {"gremlin_python.process.graph_traversal": MagicMock(__=anon_mock)},
        ):
            result = q.bi_q5_tenant_activity("local")
        assert isinstance(result, dict)
        assert "run_count" in result
        assert "edge_counts" in result


# ---------------------------------------------------------------------------
# Helm chart existence tests (no server, no live JanusGraph needed)
# ---------------------------------------------------------------------------


class TestHelmChart:
    _CHART_DIR = Path(__file__).parents[2] / "deploy" / "helm" / "janusgraph"

    def test_chart_yaml_exists(self) -> None:
        """deploy/helm/janusgraph/Chart.yaml must exist."""
        assert (self._CHART_DIR / "Chart.yaml").is_file()

    def test_chart_yaml_name_is_janusgraph(self) -> None:
        """Chart.yaml must contain 'name: janusgraph'."""
        content = (self._CHART_DIR / "Chart.yaml").read_text()
        assert "name: janusgraph" in content

    def test_values_yaml_contains_gremlin_port(self) -> None:
        """values.yaml must contain the gremlinPort key."""
        content = (self._CHART_DIR / "values.yaml").read_text()
        assert "gremlinPort" in content

    def test_deployment_yaml_exists(self) -> None:
        """templates/deployment.yaml must exist."""
        assert (self._CHART_DIR / "templates" / "deployment.yaml").is_file()

    def test_service_yaml_exists(self) -> None:
        """templates/service.yaml must exist."""
        assert (self._CHART_DIR / "templates" / "service.yaml").is_file()

    def test_configmap_yaml_exists(self) -> None:
        """templates/configmap.yaml must exist."""
        assert (self._CHART_DIR / "templates" / "configmap.yaml").is_file()

    def test_chart_appversion_is_1_0_0(self) -> None:
        """Chart.yaml appVersion must be '1.0.0' (matches JanusGraph image tag)."""
        content = (self._CHART_DIR / "Chart.yaml").read_text()
        assert 'appVersion: "1.0.0"' in content


# ---------------------------------------------------------------------------
# Integration tests — require NOVA_INTEGRATION=1 + live JanusGraph
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _INTEGRATION,
    reason="NOVA_INTEGRATION=1 not set — JanusGraph integration tests skipped",
)
class TestIntegration:
    """Full round-trip tests against a live JanusGraph container.

    Start the container before running:
        docker run -d -p 8182:8182 janusgraph/janusgraph:1.0.0

    Then run:
        NOVA_INTEGRATION=1 pytest tests/lineage/test_janusgraph.py::TestIntegration -v
    """

    def test_insert_and_provenance_round_trip(self) -> None:
        """insert + provenance must return the inserted downstream node."""
        store = JanusGraphLineageStore()
        edge = _make_edge(src="integ-src", tgt="integ-tgt", edge_type="spawned")
        store.insert(edge)
        rows = store.provenance("integ-src", depth=1)
        run_ids = [r["node_id"] for r in rows]
        assert "integ-tgt" in run_ids
        store.close()

    def test_insert_and_blast_radius_round_trip(self) -> None:
        """insert + blast_radius must return the upstream source node."""
        store = JanusGraphLineageStore()
        edge = _make_edge(src="br-src", tgt="br-tgt", edge_type="spawned")
        store.insert(edge)
        rows = store.blast_radius("br-tgt", max_depth=1)
        run_ids = [r["node_id"] for r in rows]
        assert "br-src" in run_ids
        store.close()

    def test_insert_and_replay_chain_round_trip(self) -> None:
        """insert + replay_chain must follow replayed_from edges."""
        store = JanusGraphLineageStore()
        edge = _make_edge(src="rc-v1", tgt="rc-v2", edge_type="replayed_from")
        store.insert(edge)
        rows = store.replay_chain("rc-v1")
        run_ids = [r["node_id"] for r in rows]
        assert "rc-v2" in run_ids
        store.close()
