# tests/test_lineage_importer.py
from __future__ import annotations

import json
from pathlib import Path

import yaml

from novafabric.lineage._importer import index_capsule_lineage


def _make_capsule_with_lineage(tmp_path: Path, run_id: str,
                                asset_ref: str | None = None) -> Path:
    cap = tmp_path / "runs" / run_id
    (cap / "outputs").mkdir(parents=True)
    (cap / "inputs").mkdir()

    assets: list[dict] = []
    if asset_ref:
        assets = [{"asset_ref": asset_ref, "registry": "local",
                   "asset_type": "model", "name": "foo", "version": "1.0.0"}]
    (cap / "assets.jsonl").write_text(
        "\n".join(json.dumps(a) for a in assets) + ("\n" if assets else "")
    )
    manifest = {
        "schema_version": "0.1.0",
        "run_id": run_id,
        "status": "success",
        "command": ["python", "-c", "pass"],
        "outputs": [],
    }
    (cap / "capsule.yaml").write_text(yaml.dump(manifest))

    # Write lineage.jsonl via LineageWriter
    from novafabric.lineage._writer import LineageWriter
    writer = LineageWriter(capsule_dir=cap, run_id=run_id)
    edges = writer.infer()
    writer.write(edges)
    return cap


def test_import_capsule_with_one_asset(tmp_path: Path) -> None:
    cap = _make_capsule_with_lineage(
        tmp_path, "01HXRUN0001", asset_ref="model:foo@1.0.0+sha256:abc"
    )
    db = tmp_path / "test.db"
    result = index_capsule_lineage(cap, db_path=db)
    assert not result.skipped
    assert result.capsule_run_id == "01HXRUN0001"
    assert result.edges_indexed == 1


def test_import_capsule_without_lineage_jsonl_is_skipped(tmp_path: Path) -> None:
    cap = tmp_path / "runs" / "01HXRUN0002"
    cap.mkdir(parents=True)
    (cap / "capsule.yaml").write_text("run_id: 01HXRUN0002\n")
    db = tmp_path / "test.db"
    result = index_capsule_lineage(cap, db_path=db)
    assert result.skipped is True
    assert result.edges_indexed == 0


def test_import_is_idempotent(tmp_path: Path) -> None:
    cap = _make_capsule_with_lineage(
        tmp_path, "01HXRUN0003", asset_ref="model:foo@1.0.0+sha256:abc"
    )
    db = tmp_path / "test.db"
    index_capsule_lineage(cap, db_path=db)
    index_capsule_lineage(cap, db_path=db)
    import sqlite3
    conn = sqlite3.connect(str(db))
    edge_count = conn.execute("SELECT COUNT(*) FROM lineage_edges").fetchone()[0]
    assert edge_count == 1


def test_import_batch_runs_dir(tmp_path: Path) -> None:
    from novafabric.lineage._importer import import_capsule_dir
    _make_capsule_with_lineage(tmp_path, "01HXRUN0004",
                                asset_ref="model:foo@1.0.0+sha256:abc")
    _make_capsule_with_lineage(tmp_path, "01HXRUN0005",
                                asset_ref="model:bar@2.0.0+sha256:def")
    runs_dir = tmp_path / "runs"
    db = tmp_path / "test.db"
    results = import_capsule_dir(runs_dir, db_path=db)
    assert len(results) == 2
    assert all(not r.skipped for r in results)


def test_import_capsule_warning_on_invalid_jsonl(tmp_path: Path) -> None:
    cap = tmp_path / "runs" / "01HXRUN0006"
    cap.mkdir(parents=True)
    (cap / "lineage.jsonl").write_text("not-valid-json\n")
    (cap / "capsule.yaml").write_text("run_id: 01HXRUN0006\n")
    db = tmp_path / "test.db"
    result = index_capsule_lineage(cap, db_path=db)
    assert result.skipped is True
    assert result.warning is not None


def test_import_capsule_skips_blank_lines_in_jsonl(tmp_path: Path) -> None:
    """Blank lines in lineage.jsonl must be silently skipped (line 102 branch)."""
    cap = _make_capsule_with_lineage(
        tmp_path, "01HXRUN0007", asset_ref="model:foo@1.0.0+sha256:abc"
    )
    # Append blank lines to the lineage.jsonl
    lineage_path = cap / "lineage.jsonl"
    lineage_path.write_text("\n" + lineage_path.read_text() + "\n\n")
    db = tmp_path / "test.db"
    result = index_capsule_lineage(cap, db_path=db)
    assert not result.skipped
    assert result.edges_indexed >= 1


def test_import_capsule_with_artifact_node(tmp_path: Path) -> None:
    """Imports an edge whose source is an artifact node (lines 54-61 in _importer)."""
    import yaml as _yaml

    run_id = "01HXRUN0008"
    cap = tmp_path / "runs" / run_id
    (cap / "outputs").mkdir(parents=True)
    (cap / "inputs").mkdir()
    (cap / "assets.jsonl").write_text("")
    manifest = {
        "schema_version": "0.1.0",
        "run_id": run_id,
        "status": "success",
        "command": ["python", "-c", "pass"],
        "outputs": ["outputs/result.txt"],
    }
    (cap / "capsule.yaml").write_text(_yaml.dump(manifest))
    (cap / "outputs" / "result.txt").write_text("data")

    # Build a produced_by edge manually using LineageWriter
    from novafabric.lineage._writer import LineageWriter

    writer = LineageWriter(capsule_dir=cap, run_id=run_id)
    edges = writer.infer()
    writer.write(edges)

    db = tmp_path / "test.db"
    result = index_capsule_lineage(cap, db_path=db)
    assert not result.skipped
    assert result.edges_indexed == 1  # produced_by edge
