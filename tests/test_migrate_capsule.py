"""Tests for nova migrate (capsule format v0.1.0 → v1.0.0, ADR-0034 §6)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml
from _help_assert import assert_flag_in_help
from typer.testing import CliRunner

from novafabric.capsule_migration._converter import (
    SOURCE_VERSION,
    TARGET_VERSION,
    CapsuleMigrationError,
    CapsuleMigrator,
)
from novafabric.cli.main import app

_EXAMPLE = Path(__file__).parent.parent / "examples" / "capsules" / "minimal-run"

runner = CliRunner()


@pytest.fixture()
def src(tmp_path: Path) -> Path:
    """Copy the minimal-run example capsule to tmp_path/source."""
    dest = tmp_path / "source"
    shutil.copytree(_EXAMPLE, dest)
    return dest


# ── Success cases ─────────────────────────────────────────────────────────────


def test_migrate_bumps_capsule_yaml(src: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    CapsuleMigrator().migrate(source=src, output=out)
    data = yaml.safe_load((out / "capsule.yaml").read_text())
    assert data["schema_version"] == TARGET_VERSION


def test_migrate_bumps_env_lock(src: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    CapsuleMigrator().migrate(source=src, output=out)
    data = yaml.safe_load((out / "env.lock").read_text())
    assert data["schema_version"] == TARGET_VERSION


def test_migrate_bumps_redaction_proof(src: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    CapsuleMigrator().migrate(source=src, output=out)
    data = json.loads((out / "redaction-proof.json").read_text())
    assert data["schema_version"] == TARGET_VERSION


def test_migrate_bumps_replay_yaml(src: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    CapsuleMigrator().migrate(source=src, output=out)
    data = yaml.safe_load((out / "replay.yaml").read_text())
    assert data["schema_version"] == TARGET_VERSION


def test_migrate_does_not_modify_source(src: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    CapsuleMigrator().migrate(source=src, output=out)
    data = yaml.safe_load((src / "capsule.yaml").read_text())
    assert data["schema_version"] == SOURCE_VERSION


def test_migrate_adds_lineage_edge(src: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    result = CapsuleMigrator().migrate(source=src, output=out)

    lineage_path = out / "lineage.jsonl"
    assert lineage_path.exists()
    edges = [
        json.loads(line) for line in lineage_path.read_text().splitlines() if line.strip()
    ]
    migration_edges = [e for e in edges if e.get("edge_type") == "migration_source"]
    assert len(migration_edges) == 1
    edge = migration_edges[0]
    assert edge["edge_id"] == result.lineage_edge_id
    assert edge["facets"]["from_schema_version"] == SOURCE_VERSION
    assert edge["source"]["schema_version"] == SOURCE_VERSION
    assert edge["target"]["schema_version"] == TARGET_VERSION
    assert edge["confidence"] == "high"


def test_migrate_sets_lineage_ref_when_missing(src: Path, tmp_path: Path) -> None:
    # Confirm source doesn't have lineage_ref
    capsule_data = yaml.safe_load((src / "capsule.yaml").read_text())
    assert "lineage_ref" not in capsule_data

    out = tmp_path / "out"
    CapsuleMigrator().migrate(source=src, output=out)

    out_data = yaml.safe_load((out / "capsule.yaml").read_text())
    assert out_data.get("lineage_ref") == "lineage.jsonl"


def test_migrate_returns_files_updated(src: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    result = CapsuleMigrator().migrate(source=src, output=out)
    assert "capsule.yaml" in result.files_updated
    assert "redaction-proof.json" in result.files_updated
    assert "replay.yaml" in result.files_updated
    assert "env.lock" in result.files_updated


def test_migrate_bumps_jsonl_records_with_schema_version(src: Path, tmp_path: Path) -> None:
    # Inject a JSONL record with schema_version into model-calls.jsonl
    record = {"schema_version": SOURCE_VERSION, "call_id": "test-01", "model": "test"}
    (src / "model-calls.jsonl").write_text(json.dumps(record, separators=(",", ":")) + "\n")

    out = tmp_path / "out"
    CapsuleMigrator().migrate(source=src, output=out)

    lines = [ln for ln in (out / "model-calls.jsonl").read_text().splitlines() if ln.strip()]
    assert len(lines) == 1
    bumped = json.loads(lines[0])
    assert bumped["schema_version"] == TARGET_VERSION
    assert bumped["call_id"] == "test-01"


def test_migrate_skips_jsonl_records_without_schema_version(src: Path, tmp_path: Path) -> None:
    record = {"span_id": "abc", "name": "test-span"}
    (src / "trace.jsonl").write_text(json.dumps(record) + "\n")

    out = tmp_path / "out"
    CapsuleMigrator().migrate(source=src, output=out)

    lines = [ln for ln in (out / "trace.jsonl").read_text().splitlines() if ln.strip()]
    assert len(lines) == 1
    assert json.loads(lines[0])["span_id"] == "abc"
    assert "schema_version" not in json.loads(lines[0])


# ── Error cases ───────────────────────────────────────────────────────────────


def test_migrate_error_source_not_found(tmp_path: Path) -> None:
    with pytest.raises(CapsuleMigrationError) as exc_info:
        CapsuleMigrator().migrate(source=tmp_path / "nope", output=tmp_path / "out")
    err = exc_info.value
    assert "not found" in str(err).lower()
    assert err.spec_section == "ADR-0034 §6"


def test_migrate_error_source_is_file(tmp_path: Path) -> None:
    f = tmp_path / "notadir"
    f.write_text("data")
    with pytest.raises(CapsuleMigrationError) as exc_info:
        CapsuleMigrator().migrate(source=f, output=tmp_path / "out")
    assert "directory" in str(exc_info.value).lower()


def test_migrate_error_no_capsule_yaml(tmp_path: Path) -> None:
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "other.txt").write_text("x")
    with pytest.raises(CapsuleMigrationError) as exc_info:
        CapsuleMigrator().migrate(source=bad, output=tmp_path / "out")
    err = exc_info.value
    assert "capsule.yaml" in str(err)
    assert err.field == "capsule.yaml"


def test_migrate_error_output_already_exists(src: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(CapsuleMigrationError) as exc_info:
        CapsuleMigrator().migrate(source=src, output=out)
    assert "already exists" in str(exc_info.value)


def test_migrate_error_already_v1(src: Path, tmp_path: Path) -> None:
    cap = src / "capsule.yaml"
    data = yaml.safe_load(cap.read_text())
    data["schema_version"] = TARGET_VERSION
    with open(cap, "w") as f:
        yaml.safe_dump(data, f, sort_keys=False)

    with pytest.raises(CapsuleMigrationError) as exc_info:
        CapsuleMigrator().migrate(source=src, output=tmp_path / "out")
    assert "already at" in str(exc_info.value)


def test_migrate_error_unsupported_version(src: Path, tmp_path: Path) -> None:
    cap = src / "capsule.yaml"
    data = yaml.safe_load(cap.read_text())
    data["schema_version"] = "0.2.0"
    with open(cap, "w") as f:
        yaml.safe_dump(data, f, sort_keys=False)

    with pytest.raises(CapsuleMigrationError) as exc_info:
        CapsuleMigrator().migrate(source=src, output=tmp_path / "out")
    assert "Unsupported" in str(exc_info.value)
    assert exc_info.value.field == "capsule.yaml/schema_version"


def test_migration_error_as_dict_all_fields() -> None:
    err = CapsuleMigrationError(
        "something failed",
        field="capsule.yaml/schema_version",
        rule="must be 0.1.x",
        spec_section="ADR-0034 §6",
    )
    d = err.as_dict()
    assert d["error"] == "something failed"
    assert d["field"] == "capsule.yaml/schema_version"
    assert d["rule"] == "must be 0.1.x"
    assert d["spec_section"] == "ADR-0034 §6"


def test_migration_error_as_dict_minimal() -> None:
    err = CapsuleMigrationError("bare error")
    d = err.as_dict()
    assert d["error"] == "bare error"
    assert "field" not in d
    assert "rule" not in d
    assert "spec_section" not in d


# ── CLI smoke tests ───────────────────────────────────────────────────────────


def test_cli_migrate_help() -> None:
    result = runner.invoke(app, ["migrate", "--help"])
    assert result.exit_code == 0
    assert "0.1.x" in result.output
    assert_flag_in_help(result, "--output")


def test_cli_migrate_success(src: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    result = runner.invoke(app, ["migrate", str(src), "--output", str(out)])
    assert result.exit_code == 0
    assert "Migrated" in result.output
    data = yaml.safe_load((out / "capsule.yaml").read_text())
    assert data["schema_version"] == TARGET_VERSION


def test_cli_migrate_error_exit_code(tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["migrate", str(tmp_path / "nope"), "--output", str(tmp_path / "out")]
    )
    assert result.exit_code == 1
    assert "Error" in result.output
