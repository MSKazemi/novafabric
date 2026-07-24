"""ADR-0212 — centrality metrics over the seeded lineage graph."""
from __future__ import annotations

import json

from novafabric.lineage._store import LineageStore
from novafabric.lineage._types import node_id_for
from novafabric.lineage.analytics.centrality import compute_graph_metrics

_HUB_ID = node_id_for("asset", "local:hub-model@v1")
_BRIDGE_ID = node_id_for("run", "run-bridge")


def test_hub_has_top_degree_and_ranks_in_hubs(seeded_lineage_store):
    report = compute_graph_metrics(seeded_lineage_store)
    by_id = {m.node_id: m for m in report.top_hubs}
    hub = by_id[_HUB_ID]
    assert hub.degree_in == 5  # run-1..run-4 + run-victim
    assert hub.degree_out == 0
    max_degree = max(m.degree_in + m.degree_out for m in report.top_hubs)
    assert hub.degree_in + hub.degree_out == max_degree
    # Hubs rank by connectivity (degree, then pagerank): the most-consumed
    # asset leads, not the 2-cycle that soaks up raw PageRank mass.
    assert report.top_hubs[0].node_id == _HUB_ID


def test_bridge_is_articulation_point(seeded_lineage_store):
    report = compute_graph_metrics(seeded_lineage_store)
    art_ids = {m.node_id for m in report.articulation_points}
    assert _BRIDGE_ID in art_ids
    assert all(m.is_articulation_point for m in report.articulation_points)


def test_counts_match_store(seeded_lineage_store):
    report = compute_graph_metrics(seeded_lineage_store)
    assert report.node_count == 16
    assert report.edge_count == 15
    assert report.sampled is False


def test_empty_graph_yields_empty_report(tmp_path):
    store = LineageStore(db_path=tmp_path / "empty.db")
    report = compute_graph_metrics(store)
    assert report.node_count == 0
    assert report.top_hubs == []
    assert report.articulation_points == []


def test_deterministic_json(seeded_lineage_store):
    a = json.dumps(compute_graph_metrics(seeded_lineage_store).as_dict())
    b = json.dumps(compute_graph_metrics(seeded_lineage_store).as_dict())
    assert a == b


def test_sampled_flag_flips_above_threshold(seeded_lineage_store):
    report = compute_graph_metrics(
        seeded_lineage_store, betweenness_sample_threshold=1
    )
    assert report.sampled is True
    # Still deterministic under the fixed default seed.
    again = compute_graph_metrics(
        seeded_lineage_store, betweenness_sample_threshold=1
    )
    assert json.dumps(report.as_dict()) == json.dumps(again.as_dict())


def test_top_n_caps_hub_list(seeded_lineage_store):
    report = compute_graph_metrics(seeded_lineage_store, top_n=3)
    assert len(report.top_hubs) == 3
