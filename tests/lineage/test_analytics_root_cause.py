"""ADR-0213 — upstream root-cause ranking on the seeded lineage graph."""
from __future__ import annotations

import json

import pytest

from novafabric.diagnose.attribution import AgentErrorTaxonomy
from novafabric.lineage._store import LineageStore
from novafabric.lineage._types import LineageEdge, node_id_for
from novafabric.lineage.analytics.root_cause import (
    UnknownLineageRunError,
    rank_root_causes,
)

_BAD_ID = node_id_for("run", "run-bad")
_ASSET_X_ID = node_id_for("asset", "local:stale-data@v3")


def test_erroring_upstream_node_ranks_first(seeded_lineage_store):
    report = rank_root_causes(seeded_lineage_store, "run-victim")
    assert report.responsible is not None
    assert report.responsible.node_id == _BAD_ID
    assert report.suspects[0].node_id == _BAD_ID
    # "tool call timeout" hits the SYSTEM cues of the shared ADR-0084 taxonomy.
    assert report.taxonomy == AgentErrorTaxonomy.SYSTEM


def test_correlation_signal_counts_sibling_failed_runs(seeded_lineage_store):
    report = rank_root_causes(seeded_lineage_store, "run-victim")
    by_id = {s.node_id: s for s in report.suspects}
    # run-victim2 also consumed stale-data@v3, so the asset gains correlation.
    assert any("other failed run" in sig for sig in by_id[_ASSET_X_ID].signals)


def test_inferred_confidence_reweights(seeded_lineage_store):
    report = rank_root_causes(seeded_lineage_store, "run-victim")
    by_id = {s.node_id: s for s in report.suspects}
    assert any("inferred" in sig for sig in by_id[_ASSET_X_ID].signals)


def test_unknown_run_raises(seeded_lineage_store):
    with pytest.raises(UnknownLineageRunError):
        rank_root_causes(seeded_lineage_store, "no-such-run")


def test_no_error_graph_yields_honest_empty_responsible(tmp_path):
    store = LineageStore(db_path=tmp_path / "healthy.db")
    store.insert_edge(
        LineageEdge(
            edge_type="consumed",
            source={"kind": "run", "run_id": "run-ok"},
            target={"kind": "asset", "registry": "local", "asset_ref": "a@v1"},
            confidence="observed",
            capsule_run_id="cap-1",
        )
    )
    report = rank_root_causes(store, "run-ok")
    assert report.responsible is None
    assert report.taxonomy == AgentErrorTaxonomy.UNKNOWN
    assert any("refusing to fabricate" in n for n in report.notes)
    # Healthy candidates are still listed, just never blamed.
    assert report.suspects


def test_no_provenance_is_clean(tmp_path):
    store = LineageStore(db_path=tmp_path / "leaf.db")
    store.insert_edge(
        LineageEdge(
            edge_type="produced_by",
            source={"kind": "asset", "registry": "local", "asset_ref": "b@v1"},
            target={"kind": "run", "run_id": "run-leaf"},
            confidence="observed",
            capsule_run_id="cap-2",
        )
    )
    report = rank_root_causes(store, "run-leaf")
    assert report.suspects == []
    assert report.responsible is None


def test_deterministic_json(seeded_lineage_store):
    a = json.dumps(rank_root_causes(seeded_lineage_store, "run-victim").as_dict())
    b = json.dumps(rank_root_causes(seeded_lineage_store, "run-victim").as_dict())
    assert a == b


def test_missing_capsule_dir_degrades_with_note(seeded_lineage_store, tmp_path):
    report = rank_root_causes(
        seeded_lineage_store, "run-victim", capsule_dir=tmp_path / "none"
    )
    assert report.responsible is not None
    assert any("No capsule found" in n for n in report.notes)
