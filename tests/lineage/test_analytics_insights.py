"""ADR-0215 — synthesized insights report over the seeded lineage graph."""
from __future__ import annotations

import json

from novafabric.lineage._store import LineageStore
from novafabric.lineage._types import LineageEdge, node_id_for
from novafabric.lineage.analytics.insights import build_insights_report

_HUB_ID = node_id_for("asset", "local:hub-model@v1")


def test_counts_and_orphans(seeded_lineage_store):
    report = build_insights_report(seeded_lineage_store)
    assert report.node_counts_by_kind == {"asset": 2, "external": 1, "run": 13}
    assert sum(report.edge_counts_by_type.values()) == 15
    assert report.orphan_total == 1
    assert report.orphan_nodes == ["external:orphan-dataset"]
    assert report.health["node_count"] == 16
    assert report.health["largest_component_fraction"] == round(15 / 16, 4)


def test_hubs_consistent_with_adr_0212(seeded_lineage_store):
    report = build_insights_report(seeded_lineage_store)
    assert report.top_hubs[0].node_id == _HUB_ID
    assert any(m.ref == "run-bridge" for m in report.articulation_points)


def test_communities_seeded_deterministic(seeded_lineage_store):
    a = build_insights_report(seeded_lineage_store, seed=0)
    b = build_insights_report(seeded_lineage_store, seed=0)
    assert a.communities == b.communities
    assert a.communities  # the seeded graph clusters into at least one group
    assert all(len(c) >= 2 for c in a.communities)


def test_cost_degrades_honestly_without_data(seeded_lineage_store):
    report = build_insights_report(seeded_lineage_store)
    assert report.cost_hotspots is None
    assert "unavailable" in report.cost_note


def test_cost_populates_from_node_payloads(tmp_path):
    store = LineageStore(db_path=tmp_path / "cost.db")
    store.insert_edge(
        LineageEdge(
            edge_type="consumed",
            source={"kind": "run", "run_id": "run-pricey", "cost_usd": 12.5},
            target={"kind": "asset", "registry": "local", "asset_ref": "m@v1"},
            confidence="observed",
            capsule_run_id="cap-c",
        )
    )
    report = build_insights_report(store)
    assert report.cost_hotspots == [
        {"kind": "run", "ref": "run-pricey", "cost_usd": 12.5}
    ]
    assert "payload" in report.cost_note


def test_bad_cost_db_degrades_with_note(seeded_lineage_store, tmp_path):
    bogus = tmp_path / "not-a-db.duckdb"
    bogus.write_text("junk")
    report = build_insights_report(seeded_lineage_store, cost_db=bogus)
    assert report.cost_hotspots is None
    assert str(bogus) in report.cost_note


def test_markdown_snapshot_stable(seeded_lineage_store):
    a = build_insights_report(seeded_lineage_store).to_markdown()
    b = build_insights_report(seeded_lineage_store).to_markdown()
    assert a == b
    assert "# NovaFabric graph insights" in a
    assert "Top hubs" in a and "Orphans" in a


def test_json_deterministic(seeded_lineage_store):
    a = json.dumps(build_insights_report(seeded_lineage_store).as_dict())
    b = json.dumps(build_insights_report(seeded_lineage_store).as_dict())
    assert a == b


def test_empty_store_yields_empty_report(tmp_path):
    report = build_insights_report(LineageStore(db_path=tmp_path / "e.db"))
    assert report.health["node_count"] == 0
    assert report.communities == []
    assert report.top_hubs == []
