"""E-3 tests: migration kit — migrate() (deprecated), migrate_from_ocs(), dry-run, divergence."""

from __future__ import annotations

import io
import json
import warnings
import zipfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from novafabric.lineage._types import LineageEdge
from novafabric.lineage.backends.sqlite import SqliteLineageStore
from novafabric.lineage.migration.kit import (
    MigrationDivergenceError,
    _edges_from_capsule_bytes,
    _edges_from_lineage_jsonl,
    migrate,
    migrate_from_ocs,
)
from novafabric.lineage.migration.parquet_replay import jsonl_to_parquet


def _make_edge_jsonl_dict(i: int) -> dict:
    return {
        "edge_id": f"edge-{i:04d}",
        "edge_type": "contains",
        "source": {"kind": "run", "run_id": f"run-src-{i}"},
        "target": {"kind": "run", "run_id": f"run-tgt-{i}"},
        "confidence": "high",
        "capsule_run_id": f"capsule-{i % 5}",
        "schema_version": "0.1.0",
        "created_at": "2026-01-01T00:00:00.000000Z",
    }


def _build_parquet(tmp_path: Path, count: int) -> Path:
    jsonl = tmp_path / "edges.jsonl"
    with jsonl.open("w") as fh:
        for i in range(count):
            fh.write(json.dumps(_make_edge_jsonl_dict(i)) + "\n")
    parquet = tmp_path / "edges.parquet"
    return jsonl_to_parquet(jsonl, parquet)


def _make_target(tmp_path: Path, suffix: str = "") -> SqliteLineageStore:
    return SqliteLineageStore(db_path=tmp_path / f"target{suffix}.db")


def _call_migrate(*args, **kwargs):
    """Call the deprecated migrate() suppressing the DeprecationWarning."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        return migrate(*args, **kwargs)


class TestMigrateBasic:
    def test_returns_dict(self, tmp_path: Path) -> None:
        parquet = _build_parquet(tmp_path, 10)
        tgt = _make_target(tmp_path)
        result = _call_migrate(parquet, tgt, validate=False, commit=False)
        assert isinstance(result, dict)

    def test_loaded_count(self, tmp_path: Path) -> None:
        parquet = _build_parquet(tmp_path, 100)
        tgt = _make_target(tmp_path)
        result = _call_migrate(parquet, tgt, validate=False, commit=False)
        assert result["loaded"] == 100

    def test_dry_run_not_committed(self, tmp_path: Path) -> None:
        parquet = _build_parquet(tmp_path, 10)
        tgt = _make_target(tmp_path)
        result = _call_migrate(parquet, tgt, validate=False, commit=False)
        assert result["committed"] is False

    def test_dry_run_store_empty(self, tmp_path: Path) -> None:
        parquet = _build_parquet(tmp_path, 10)
        tgt = _make_target(tmp_path)
        _call_migrate(parquet, tgt, validate=False, commit=False)
        # Store should still be empty after dry run
        rows = tgt.provenance("run-src-0", depth=5)
        assert rows == []

    def test_commit_true_returns_committed(self, tmp_path: Path) -> None:
        parquet = _build_parquet(tmp_path, 10)
        tgt = _make_target(tmp_path)
        result = _call_migrate(parquet, tgt, validate=True, commit=True)
        assert result["committed"] is True

    def test_commit_loaded_matches(self, tmp_path: Path) -> None:
        parquet = _build_parquet(tmp_path, 50)
        tgt = _make_target(tmp_path)
        result = _call_migrate(parquet, tgt, validate=True, commit=True)
        assert result["loaded"] == 50

    def test_commit_zero_diverged(self, tmp_path: Path) -> None:
        parquet = _build_parquet(tmp_path, 20)
        tgt = _make_target(tmp_path)
        result = _call_migrate(parquet, tgt, validate=True, commit=True)
        assert result["diverged"] == 0

    def test_edges_in_store_after_commit(self, tmp_path: Path) -> None:
        parquet = _build_parquet(tmp_path, 10)
        tgt = _make_target(tmp_path)
        _call_migrate(parquet, tgt, validate=True, commit=True)
        # After commit, run-src-0 should have provenance to run-tgt-0
        rows = tgt.provenance("run-src-0", depth=5)
        assert len(rows) >= 1

    def test_no_validate_still_commits(self, tmp_path: Path) -> None:
        parquet = _build_parquet(tmp_path, 5)
        tgt = _make_target(tmp_path)
        result = _call_migrate(parquet, tgt, validate=False, commit=True)
        assert result["committed"] is True
        assert result["loaded"] == 5

    def test_migrate_emits_deprecation_warning(self, tmp_path: Path) -> None:
        """migrate() must emit DeprecationWarning to guide users toward migrate_from_ocs()."""
        parquet = _build_parquet(tmp_path, 3)
        tgt = _make_target(tmp_path, "_dep")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            migrate(parquet, tgt, validate=False, commit=False)
        deprecations = [w for w in caught if issubclass(w.category, DeprecationWarning)]
        assert len(deprecations) >= 1
        assert "migrate_from_ocs" in str(deprecations[0].message)


class TestMigrateDivergence:
    def test_divergence_raises_on_bad_target(self, tmp_path: Path) -> None:
        """Target store pre-loaded with wrong edges should trigger divergence."""
        # Build parquet with edges run-src-0 -> run-tgt-0
        parquet = _build_parquet(tmp_path, 5)
        # Target has a completely different edge that doesn't match source
        tgt = _make_target(tmp_path, "_bad")
        # Insert a conflicting edge that will make counts differ
        # We pre-insert an extra node so provenance counts differ from what's
        # reconstructed from the source Parquet
        extra_edge = LineageEdge(
            edge_type="contains",
            source={"kind": "run", "run_id": "run-src-0"},
            target={"kind": "run", "run_id": "run-extra-unexpected"},
            confidence="high",
            capsule_run_id="capsule-extra",
        )
        tgt.insert(extra_edge)
        # Now migrate into the same target — after loading, the target has MORE
        # edges than the pure source reconstruction, causing divergence.
        with pytest.raises(MigrationDivergenceError):
            _call_migrate(parquet, tgt, validate=True, commit=True)

    def test_migration_divergence_error_is_exception(self) -> None:
        assert issubclass(MigrationDivergenceError, Exception)

    def test_validate_false_no_error_even_with_bad_data(self, tmp_path: Path) -> None:
        parquet = _build_parquet(tmp_path, 5)
        tgt = _make_target(tmp_path, "_noval")
        tgt.insert(
            LineageEdge(
                edge_type="contains",
                source={"kind": "run", "run_id": "run-src-0"},
                target={"kind": "run", "run_id": "run-extra"},
                confidence="high",
                capsule_run_id="capsule-x",
            )
        )
        # validate=False means no divergence check — should not raise
        result = _call_migrate(parquet, tgt, validate=False, commit=True)
        assert result["committed"] is True


# ---------------------------------------------------------------------------
# Helpers for OCS-backed migration tests
# ---------------------------------------------------------------------------


def _make_lineage_jsonl_bytes(edges: list[dict]) -> bytes:
    """Serialise a list of edge dicts to lineage.jsonl bytes."""
    lines = [json.dumps(e) + "\n" for e in edges]
    return "".join(lines).encode("utf-8")


def _make_capsule_zip(lineage_bytes: bytes | None = None) -> bytes:
    """Build a minimal capsule ZIP archive with an optional lineage.jsonl."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        # Always include a minimal capsule.yaml
        zf.writestr("capsule.yaml", "run_id: test-run\nschema_version: '0.2.0'\n")
        if lineage_bytes is not None:
            zf.writestr("lineage.jsonl", lineage_bytes.decode("utf-8"))
    return buf.getvalue()


def _make_ocs_stub(
    run_id: str, capsule_uri: str, capsule_bytes: bytes
) -> MagicMock:
    """Return a minimal stub ObjectCapsuleStore for one run with one capsule."""
    ocs = MagicMock()
    ocs.list_capsules.return_value = [capsule_uri]
    ocs.get_capsule.return_value = capsule_bytes
    return ocs


# ---------------------------------------------------------------------------
# _edges_from_lineage_jsonl unit tests
# ---------------------------------------------------------------------------


class TestEdgesFromLineageJsonl:
    def test_returns_edge_list(self) -> None:
        edge_dict = {
            "edge_id": "edge-0001",
            "edge_type": "contains",
            "source": {"kind": "run", "run_id": "run-a"},
            "target": {"kind": "run", "run_id": "run-b"},
            "confidence": "high",
            "capsule_run_id": "cap-x",
            "schema_version": "0.1.0",
            "created_at": "2026-01-01T00:00:00Z",
        }
        data = json.dumps(edge_dict) + "\n"
        edges = _edges_from_lineage_jsonl(data, capsule_run_id="cap-x")
        assert len(edges) == 1
        assert edges[0].edge_type == "contains"

    def test_empty_data_returns_empty(self) -> None:
        edges = _edges_from_lineage_jsonl("", capsule_run_id="cap-x")
        assert edges == []

    def test_blank_lines_skipped(self) -> None:
        edge_dict = {
            "edge_id": "e1", "edge_type": "contains",
            "source": {"kind": "run", "run_id": "r-a"},
            "target": {"kind": "run", "run_id": "r-b"},
            "confidence": "high", "capsule_run_id": "c-1",
            "schema_version": "0.1.0", "created_at": "2026-01-01T00:00:00Z",
        }
        data = "\n" + json.dumps(edge_dict) + "\n\n"
        edges = _edges_from_lineage_jsonl(data, capsule_run_id="c-1")
        assert len(edges) == 1

    def test_malformed_line_is_skipped(self) -> None:
        data = "not-valid-json\n"
        edges = _edges_from_lineage_jsonl(data, capsule_run_id="c-1")
        assert edges == []

    def test_accepts_bytes_input(self) -> None:
        edge_dict = {
            "edge_id": "e2", "edge_type": "contains",
            "source": {"kind": "run", "run_id": "r-a"},
            "target": {"kind": "run", "run_id": "r-b"},
            "confidence": "high", "capsule_run_id": "c-2",
            "schema_version": "0.1.0", "created_at": "2026-01-01T00:00:00Z",
        }
        data_bytes = (json.dumps(edge_dict) + "\n").encode("utf-8")
        edges = _edges_from_lineage_jsonl(data_bytes, capsule_run_id="c-2")
        assert len(edges) == 1


# ---------------------------------------------------------------------------
# _edges_from_capsule_bytes unit tests
# ---------------------------------------------------------------------------


class TestEdgesFromCapsuleBytes:
    def test_extracts_edges_from_zip(self) -> None:
        edge_dict = {
            "edge_id": "e-cap-1", "edge_type": "contains",
            "source": {"kind": "run", "run_id": "run-a"},
            "target": {"kind": "run", "run_id": "run-b"},
            "confidence": "high", "capsule_run_id": "run-x",
            "schema_version": "0.1.0", "created_at": "2026-01-01T00:00:00Z",
        }
        lineage_bytes = _make_lineage_jsonl_bytes([edge_dict])
        capsule_zip = _make_capsule_zip(lineage_bytes)
        edges = _edges_from_capsule_bytes(capsule_zip, capsule_run_id="run-x")
        assert len(edges) == 1
        assert edges[0].edge_id == "e-cap-1"

    def test_capsule_without_lineage_returns_empty(self) -> None:
        capsule_zip = _make_capsule_zip(lineage_bytes=None)
        edges = _edges_from_capsule_bytes(capsule_zip, capsule_run_id="run-y")
        assert edges == []

    def test_bad_zip_returns_empty(self) -> None:
        edges = _edges_from_capsule_bytes(b"not a zip archive", capsule_run_id="run-z")
        assert edges == []


# ---------------------------------------------------------------------------
# migrate_from_ocs() integration tests (using mock OCS)
# ---------------------------------------------------------------------------


class TestMigrateFromOcs:
    def test_dry_run_returns_correct_dict(self, tmp_path: Path) -> None:
        """migrate_from_ocs() dry-run returns loaded count without writing to store."""
        edge_dict = {
            "edge_id": "ocs-edge-0001", "edge_type": "contains",
            "source": {"kind": "run", "run_id": "run-src-ocs"},
            "target": {"kind": "run", "run_id": "run-tgt-ocs"},
            "confidence": "high", "capsule_run_id": "run-ocs-1",
            "schema_version": "0.1.0", "created_at": "2026-01-01T00:00:00Z",
        }
        lineage_bytes = _make_lineage_jsonl_bytes([edge_dict])
        capsule_zip = _make_capsule_zip(lineage_bytes)
        ocs = _make_ocs_stub("run-ocs-1", "capsules/org/0000/run-ocs-1/data.zip", capsule_zip)

        tgt = SqliteLineageStore(db_path=tmp_path / "tgt_dry.db")
        result = migrate_from_ocs(
            ocs=ocs, tenant="org", dest_store=tgt,
            run_ids=["run-ocs-1"],
            validate=False, commit=False,
        )

        assert result["loaded"] == 1
        assert result["committed"] is False
        assert result["capsules_scanned"] == 1
        # Nothing written to store in dry-run mode
        assert tgt.provenance("run-src-ocs", depth=5) == []

    def test_commit_writes_edges_to_store(self, tmp_path: Path) -> None:
        """migrate_from_ocs(commit=True) inserts lineage edges into dest_store."""
        edge_dict = {
            "edge_id": "ocs-edge-0002", "edge_type": "contains",
            "source": {"kind": "run", "run_id": "run-src-ocs2"},
            "target": {"kind": "run", "run_id": "run-tgt-ocs2"},
            "confidence": "high", "capsule_run_id": "run-ocs-2",
            "schema_version": "0.1.0", "created_at": "2026-01-01T00:00:00Z",
        }
        lineage_bytes = _make_lineage_jsonl_bytes([edge_dict])
        capsule_zip = _make_capsule_zip(lineage_bytes)
        ocs = _make_ocs_stub("run-ocs-2", "capsules/org/0000/run-ocs-2/data.zip", capsule_zip)

        tgt = SqliteLineageStore(db_path=tmp_path / "tgt_commit.db")
        result = migrate_from_ocs(
            ocs=ocs, tenant="org", dest_store=tgt,
            run_ids=["run-ocs-2"],
            validate=False, commit=True,
        )

        assert result["loaded"] == 1
        assert result["committed"] is True
        assert result["capsules_scanned"] == 1
        rows = tgt.provenance("run-src-ocs2", depth=5)
        assert len(rows) >= 1

    def test_capsule_without_lineage_is_scanned_but_zero_loaded(self, tmp_path: Path) -> None:
        """Capsules that have no lineage.jsonl contribute 0 edges but count as scanned."""
        capsule_zip = _make_capsule_zip(lineage_bytes=None)
        ocs = _make_ocs_stub("run-no-lin", "capsules/org/0000/run-no-lin/data.zip", capsule_zip)

        tgt = SqliteLineageStore(db_path=tmp_path / "tgt_no_lin.db")
        result = migrate_from_ocs(
            ocs=ocs, tenant="org", dest_store=tgt,
            run_ids=["run-no-lin"],
            validate=False, commit=True,
        )

        assert result["loaded"] == 0
        assert result["capsules_scanned"] == 1
        assert result["committed"] is True

    def test_empty_run_ids_returns_zero(self, tmp_path: Path) -> None:
        """migrate_from_ocs() with empty run_ids returns immediately with zero counts."""
        ocs = MagicMock()
        tgt = SqliteLineageStore(db_path=tmp_path / "tgt_empty.db")
        result = migrate_from_ocs(
            ocs=ocs, tenant="org", dest_store=tgt,
            run_ids=[],
            validate=False, commit=True,
        )
        assert result["loaded"] == 0
        assert result["capsules_scanned"] == 0
        assert result["committed"] is False
        ocs.list_capsules.assert_not_called()

    def test_multiple_run_ids_aggregated(self, tmp_path: Path) -> None:
        """migrate_from_ocs() aggregates edges from all provided run_ids."""
        def make_edge(i: int, run_id: str) -> dict:
            return {
                "edge_id": f"ocs-edge-{i:04d}", "edge_type": "contains",
                "source": {"kind": "run", "run_id": f"run-src-{i}"},
                "target": {"kind": "run", "run_id": f"run-tgt-{i}"},
                "confidence": "high", "capsule_run_id": run_id,
                "schema_version": "0.1.0", "created_at": "2026-01-01T00:00:00Z",
            }

        run_ids = ["run-m1", "run-m2", "run-m3"]
        ocs = MagicMock()

        def list_capsules_side_effect(tenant, run_id):
            return [f"capsules/org/0000/{run_id}/data.zip"]

        def get_capsule_side_effect(tenant, run_id, capsule_uri):
            i = run_ids.index(run_id)
            lineage_bytes = _make_lineage_jsonl_bytes([make_edge(i, run_id)])
            return _make_capsule_zip(lineage_bytes)

        ocs.list_capsules.side_effect = list_capsules_side_effect
        ocs.get_capsule.side_effect = get_capsule_side_effect

        tgt = SqliteLineageStore(db_path=tmp_path / "tgt_multi.db")
        result = migrate_from_ocs(
            ocs=ocs, tenant="org", dest_store=tgt,
            run_ids=run_ids,
            validate=False, commit=True,
        )

        assert result["loaded"] == 3
        assert result["capsules_scanned"] == 3
        assert result["committed"] is True

    def test_validate_true_no_divergence(self, tmp_path: Path) -> None:
        """migrate_from_ocs(validate=True) does not raise when source and dest match."""
        edge_dict = {
            "edge_id": "ocs-val-001", "edge_type": "contains",
            "source": {"kind": "run", "run_id": "run-src-v"},
            "target": {"kind": "run", "run_id": "run-tgt-v"},
            "confidence": "high", "capsule_run_id": "run-val",
            "schema_version": "0.1.0", "created_at": "2026-01-01T00:00:00Z",
        }
        lineage_bytes = _make_lineage_jsonl_bytes([edge_dict])
        capsule_zip = _make_capsule_zip(lineage_bytes)
        ocs = _make_ocs_stub("run-val", "capsules/org/0000/run-val/data.zip", capsule_zip)

        tgt = SqliteLineageStore(db_path=tmp_path / "tgt_val.db")
        result = migrate_from_ocs(
            ocs=ocs, tenant="org", dest_store=tgt,
            run_ids=["run-val"],
            validate=True, commit=True,
        )
        assert result["diverged"] == 0
        assert result["committed"] is True
