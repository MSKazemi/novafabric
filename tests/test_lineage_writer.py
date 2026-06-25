# tests/test_lineage_writer.py
from __future__ import annotations

import json
from pathlib import Path

import yaml

from novafabric.lineage._writer import LineageWriter


def _make_capsule(tmp_path: Path, *, assets: list[dict] | None = None,
                  outputs: list[str] | None = None,
                  replay_of: str | None = None) -> Path:
    cap = tmp_path / "runs" / "01HXRUN000000000000001"
    cap.mkdir(parents=True)
    (cap / "outputs").mkdir()
    (cap / "inputs").mkdir()

    # assets.jsonl
    asset_records = assets or []
    (cap / "assets.jsonl").write_text(
        "\n".join(json.dumps(a) for a in asset_records) + ("\n" if asset_records else "")
    )

    # capsule.yaml
    manifest: dict = {
        "schema_version": "0.1.0",
        "run_id": "01HXRUN000000000000001",
        "status": "success",
        "command": ["python", "-c", "pass"],
        "outputs": outputs or [],
    }
    if replay_of:
        manifest["replay_of_run_id"] = replay_of
    (cap / "capsule.yaml").write_text(yaml.dump(manifest))

    # create output files listed
    for out in (outputs or []):
        (cap / out).write_text("content")

    return cap


def test_no_edges_when_capsule_is_empty(tmp_path: Path) -> None:
    cap = _make_capsule(tmp_path)
    writer = LineageWriter(capsule_dir=cap, run_id="01HXRUN000000000000001")
    edges = writer.infer()
    assert edges == []


def test_consumed_edge_from_assets_jsonl(tmp_path: Path) -> None:
    assets = [{"asset_type": "model", "name": "foo", "version": "1.0.0",
               "registry": "local", "asset_ref": "model:foo@1.0.0+sha256:abc"}]
    cap = _make_capsule(tmp_path, assets=assets)
    writer = LineageWriter(capsule_dir=cap, run_id="01HXRUN000000000000001")
    edges = writer.infer()
    assert len(edges) == 1
    assert edges[0].edge_type == "consumed"
    assert edges[0].source["kind"] == "run"
    assert edges[0].target["kind"] == "asset"
    assert edges[0].confidence == "observed"
    assert edges[0].capsule_run_id == "01HXRUN000000000000001"


def test_produced_by_edge_from_outputs(tmp_path: Path) -> None:
    cap = _make_capsule(tmp_path, outputs=["outputs/result.txt"])
    writer = LineageWriter(capsule_dir=cap, run_id="01HXRUN000000000000001")
    edges = writer.infer()
    assert len(edges) == 1
    assert edges[0].edge_type == "produced_by"
    assert edges[0].source["kind"] == "artifact"
    assert edges[0].target["kind"] == "run"
    assert edges[0].confidence == "inferred"


def test_replayed_from_edge_when_replay_metadata_present(tmp_path: Path) -> None:
    cap = _make_capsule(tmp_path, replay_of="01HXORIGRUN00000000001")
    writer = LineageWriter(capsule_dir=cap, run_id="01HXRUN000000000000001")
    edges = writer.infer()
    assert len(edges) == 1
    assert edges[0].edge_type == "replayed_from"
    assert edges[0].source["kind"] == "run"
    assert edges[0].target["kind"] == "run"
    assert edges[0].confidence == "declared"


def test_all_three_edge_types_combined(tmp_path: Path) -> None:
    assets = [{"asset_ref": "model:foo@1.0.0+sha256:abc", "registry": "local",
               "asset_type": "model", "name": "foo", "version": "1.0.0"}]
    cap = _make_capsule(tmp_path, assets=assets,
                        outputs=["outputs/result.txt"],
                        replay_of="01HXORIGRUN00000000001")
    writer = LineageWriter(capsule_dir=cap, run_id="01HXRUN000000000000001")
    edges = writer.infer()
    edge_types = {e.edge_type for e in edges}
    assert edge_types == {"consumed", "produced_by", "replayed_from"}


def test_write_creates_lineage_jsonl(tmp_path: Path) -> None:
    assets = [{"asset_ref": "model:foo@1.0.0+sha256:abc", "registry": "local",
               "asset_type": "model", "name": "foo", "version": "1.0.0"}]
    cap = _make_capsule(tmp_path, assets=assets)
    writer = LineageWriter(capsule_dir=cap, run_id="01HXRUN000000000000001")
    edges = writer.infer()
    path = writer.write(edges)
    assert path == cap / "lineage.jsonl"
    lines = [ln for ln in path.read_text().splitlines() if ln.strip()]
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["edge_type"] == "consumed"
    assert record["schema_version"] == "0.1.0"


def test_write_empty_edges_creates_empty_file(tmp_path: Path) -> None:
    cap = _make_capsule(tmp_path)
    writer = LineageWriter(capsule_dir=cap, run_id="01HXRUN000000000000001")
    path = writer.write([])
    assert path.exists()
    assert path.read_text().strip() == ""


# --- Error / edge-case paths for coverage ---

def test_no_edges_when_assets_jsonl_missing(tmp_path: Path) -> None:
    """_consumed_edges: assets.jsonl does not exist → returns []."""
    cap = _make_capsule(tmp_path)
    (cap / "assets.jsonl").unlink()  # remove the file created by _make_capsule
    writer = LineageWriter(capsule_dir=cap, run_id="01HXRUN000000000000001")
    edges = writer.infer()
    assert edges == []


def test_no_edges_when_assets_jsonl_has_invalid_json(tmp_path: Path) -> None:
    """_consumed_edges: invalid JSON lines are skipped gracefully."""
    cap = _make_capsule(tmp_path)
    (cap / "assets.jsonl").write_text("not-valid-json\n")
    writer = LineageWriter(capsule_dir=cap, run_id="01HXRUN000000000000001")
    edges = writer.infer()
    assert edges == []


def test_no_edges_when_asset_ref_is_empty(tmp_path: Path) -> None:
    """_consumed_edges: record without asset_ref is skipped."""
    cap = _make_capsule(tmp_path)
    (cap / "assets.jsonl").write_text(
        json.dumps({"registry": "local", "asset_type": "model"}) + "\n"
    )
    writer = LineageWriter(capsule_dir=cap, run_id="01HXRUN000000000000001")
    edges = writer.infer()
    assert edges == []


def test_no_produced_edges_when_capsule_yaml_missing(tmp_path: Path) -> None:
    """_produced_by_edges: capsule.yaml missing → returns []."""
    cap = _make_capsule(tmp_path, outputs=["outputs/out.txt"])
    (cap / "capsule.yaml").unlink()
    writer = LineageWriter(capsule_dir=cap, run_id="01HXRUN000000000000001")
    edges = writer.infer()
    # No capsule.yaml → no produced_by and no replayed_from edges
    assert edges == []


def test_no_produced_edges_when_capsule_yaml_is_invalid_yaml(tmp_path: Path) -> None:
    """_produced_by_edges: unreadable YAML → returns []."""
    cap = _make_capsule(tmp_path)
    (cap / "capsule.yaml").write_text("key: [unclosed")
    writer = LineageWriter(capsule_dir=cap, run_id="01HXRUN000000000000001")
    edges = writer.infer()
    assert edges == []


def test_no_replay_edge_when_capsule_yaml_missing_for_replay(tmp_path: Path) -> None:
    """_replayed_from_edge: capsule.yaml missing → returns None."""
    cap = _make_capsule(tmp_path, replay_of="01HXORIGRUN00000000001")
    (cap / "capsule.yaml").unlink()
    writer = LineageWriter(capsule_dir=cap, run_id="01HXRUN000000000000001")
    edges = writer.infer()
    assert edges == []


def test_no_replay_edge_when_capsule_yaml_is_invalid_yaml_for_replay(
    tmp_path: Path,
) -> None:
    """_replayed_from_edge: unreadable YAML → returns None."""
    cap = _make_capsule(tmp_path)
    (cap / "capsule.yaml").write_text("key: [unclosed")
    writer = LineageWriter(capsule_dir=cap, run_id="01HXRUN000000000000001")
    edges = writer.infer()
    assert edges == []
