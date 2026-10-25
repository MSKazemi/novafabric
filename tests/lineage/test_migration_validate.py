"""E-2 tests: validate_stores divergence checker."""

from __future__ import annotations

from pathlib import Path

from novafabric.lineage._types import LineageEdge
from novafabric.lineage.backends.sqlite import SqliteLineageStore
from novafabric.lineage.migration.validate import DivergenceReport, validate_stores


def _make_store(tmp_path: Path, suffix: str = "") -> SqliteLineageStore:
    return SqliteLineageStore(db_path=tmp_path / f"lineage{suffix}.db")


def _edge(src: str, tgt: str, etype: str = "contains", capsule: str = "cap-1") -> LineageEdge:
    return LineageEdge(
        edge_type=etype,
        source={"kind": "run", "run_id": src},
        target={"kind": "run", "run_id": tgt},
        confidence="high",
        capsule_run_id=capsule,
    )


class TestValidateStoresIdentical:
    def test_zero_divergence_same_edges(self, tmp_path: Path) -> None:
        src = _make_store(tmp_path, "_src")
        tgt = _make_store(tmp_path, "_tgt")
        edge = _edge("run-A", "run-B")
        src.insert(edge)
        tgt.insert(edge)
        reports = validate_stores(src, tgt, ["run-A"], max_depth=3)
        assert reports == []

    def test_zero_divergence_chain(self, tmp_path: Path) -> None:
        src = _make_store(tmp_path, "_src")
        tgt = _make_store(tmp_path, "_tgt")
        for store in (src, tgt):
            store.insert(_edge("run-A", "run-B", "contains"))
            store.insert(_edge("run-B", "run-C", "spawned"))
        reports = validate_stores(src, tgt, ["run-A", "run-B"], max_depth=3)
        assert reports == []

    def test_both_empty_no_divergence(self, tmp_path: Path) -> None:
        src = _make_store(tmp_path, "_src")
        tgt = _make_store(tmp_path, "_tgt")
        reports = validate_stores(src, tgt, ["run-unknown"], max_depth=3)
        assert reports == []

    def test_returns_list(self, tmp_path: Path) -> None:
        src = _make_store(tmp_path, "_src")
        tgt = _make_store(tmp_path, "_tgt")
        result = validate_stores(src, tgt, [], max_depth=3)
        assert isinstance(result, list)


class TestValidateStoresDiverged:
    def test_missing_edge_in_target_reported(self, tmp_path: Path) -> None:
        src = _make_store(tmp_path, "_src")
        tgt = _make_store(tmp_path, "_tgt")
        edge = _edge("run-A", "run-B")
        src.insert(edge)
        # tgt has no edges
        reports = validate_stores(src, tgt, ["run-A"], max_depth=3)
        assert len(reports) > 0

    def test_divergence_report_fields(self, tmp_path: Path) -> None:
        src = _make_store(tmp_path, "_src")
        tgt = _make_store(tmp_path, "_tgt")
        src.insert(_edge("run-A", "run-B"))
        reports = validate_stores(src, tgt, ["run-A"], max_depth=3)
        assert all(isinstance(r, DivergenceReport) for r in reports)

    def test_divergence_report_is_diverged_flag(self, tmp_path: Path) -> None:
        src = _make_store(tmp_path, "_src")
        tgt = _make_store(tmp_path, "_tgt")
        src.insert(_edge("run-A", "run-B"))
        reports = validate_stores(src, tgt, ["run-A"], max_depth=3)
        assert all(r.is_diverged for r in reports)

    def test_divergence_report_run_id(self, tmp_path: Path) -> None:
        src = _make_store(tmp_path, "_src")
        tgt = _make_store(tmp_path, "_tgt")
        src.insert(_edge("run-A", "run-B"))
        reports = validate_stores(src, tgt, ["run-A"], max_depth=3)
        assert all(r.run_id == "run-A" for r in reports)

    def test_divergence_query_type_values(self, tmp_path: Path) -> None:
        src = _make_store(tmp_path, "_src")
        tgt = _make_store(tmp_path, "_tgt")
        src.insert(_edge("run-A", "run-B"))
        reports = validate_stores(src, tgt, ["run-A"], max_depth=3)
        qtypes = {r.query_type for r in reports}
        assert qtypes <= {"provenance", "blast_radius", "replay_chain"}

    def test_empty_target_all_queries_diverge(self, tmp_path: Path) -> None:
        src = _make_store(tmp_path, "_src")
        tgt = _make_store(tmp_path, "_tgt")
        src.insert(_edge("run-X", "run-Y"))
        src.insert(_edge("run-Y", "run-Z", "replayed_from"))
        reports = validate_stores(src, tgt, ["run-X"], max_depth=5)
        # At minimum provenance diverges (src has run-Y, tgt has nothing)
        assert len(reports) >= 1

    def test_source_count_gt_zero_when_diverged(self, tmp_path: Path) -> None:
        src = _make_store(tmp_path, "_src")
        tgt = _make_store(tmp_path, "_tgt")
        src.insert(_edge("run-A", "run-B"))
        reports = validate_stores(src, tgt, ["run-A"], max_depth=3)
        prov_reports = [r for r in reports if r.query_type == "provenance"]
        if prov_reports:
            assert prov_reports[0].source_count > 0
            assert prov_reports[0].target_count == 0

    def test_multiple_run_ids(self, tmp_path: Path) -> None:
        src = _make_store(tmp_path, "_src")
        tgt = _make_store(tmp_path, "_tgt")
        src.insert(_edge("run-A", "run-B"))
        src.insert(_edge("run-C", "run-D"))
        reports = validate_stores(src, tgt, ["run-A", "run-C"], max_depth=3)
        run_ids_reported = {r.run_id for r in reports}
        assert "run-A" in run_ids_reported or "run-C" in run_ids_reported


class TestSameCountDifferentIdentity:
    """review replay-lineage-registry-18: the divergence check compared row counts, so
    a target holding different nodes with the same count reported no divergence and
    the migration committed."""

    def test_different_nodes_with_the_same_count_are_diverged(self, tmp_path: Path) -> None:
        src = _make_store(tmp_path, "_src")
        tgt = _make_store(tmp_path, "_tgt")
        src.insert(_edge("run-A", "run-B"))
        tgt.insert(_edge("run-A", "run-OTHER"))
        reports = validate_stores(src, tgt, ["run-A"], max_depth=3)
        assert reports and all(r.is_diverged for r in reports)
        assert all(r.source_count == r.target_count == 1 for r in reports if r.query_type == "provenance")
        prov = next(r for r in reports if r.query_type == "provenance")
        assert ("run", "run-B") in prov.missing_in_target
        assert ("run", "run-OTHER") in prov.unexpected_in_target

    def test_the_same_nodes_in_a_different_order_are_not_diverged(self, tmp_path: Path) -> None:
        src = _make_store(tmp_path, "_src")
        tgt = _make_store(tmp_path, "_tgt")
        for store, order in ((src, ("B", "C")), (tgt, ("C", "B"))):
            for name in order:
                store.insert(_edge("run-A", f"run-{name}"))
        assert validate_stores(src, tgt, ["run-A"], max_depth=3) == []

    def test_the_listed_identities_are_bounded(self, tmp_path: Path) -> None:
        src = _make_store(tmp_path, "_src")
        tgt = _make_store(tmp_path, "_tgt")
        for i in range(40):
            src.insert(_edge("run-A", f"run-S{i}"))
            tgt.insert(_edge("run-A", f"run-T{i}"))
        prov = next(r for r in validate_stores(src, tgt, ["run-A"]) if r.query_type == "provenance")
        assert len(prov.missing_in_target) <= 20 and len(prov.unexpected_in_target) <= 20
