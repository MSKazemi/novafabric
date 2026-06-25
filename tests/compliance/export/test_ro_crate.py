"""Tests for RO-Crate v1.1 export — library + CLI."""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from novafabric.cli.main import app
from novafabric.compliance.export.ro_crate import export_ro_crate


class TestExportRoCrateLibrary:
    def test_produces_zip(self, minimal_capsule_dir: Path, tmp_path: Path) -> None:
        out = tmp_path / "test.rocrate.zip"
        result = export_ro_crate(minimal_capsule_dir, out)
        assert result == out
        assert out.exists()
        assert zipfile.is_zipfile(out)

    def test_metadata_json_present(self, minimal_capsule_dir: Path, tmp_path: Path) -> None:
        out = tmp_path / "test.rocrate.zip"
        export_ro_crate(minimal_capsule_dir, out)
        with zipfile.ZipFile(out) as zf:
            names = zf.namelist()
        assert "ro-crate-metadata.json" in names

    def test_metadata_conforms_to_spec(self, minimal_capsule_dir: Path, tmp_path: Path) -> None:
        out = tmp_path / "test.rocrate.zip"
        export_ro_crate(minimal_capsule_dir, out)
        with zipfile.ZipFile(out) as zf:
            meta = json.loads(zf.read("ro-crate-metadata.json"))
        assert "@context" in meta
        assert "@graph" in meta
        # Root Data Entity descriptor must declare conformance
        root_desc = next(
            (n for n in meta["@graph"] if n.get("@type") == "CreativeWork"),
            None,
        )
        assert root_desc is not None
        assert "conformsTo" in root_desc

    def test_capsule_yaml_included(self, minimal_capsule_dir: Path, tmp_path: Path) -> None:
        out = tmp_path / "test.rocrate.zip"
        export_ro_crate(minimal_capsule_dir, out)
        with zipfile.ZipFile(out) as zf:
            names = zf.namelist()
        assert "capsule.yaml" in names

    def test_missing_capsule_dir_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            export_ro_crate(tmp_path / "nonexistent", tmp_path / "out.zip")

    def test_file_not_dir_raises(self, tmp_path: Path) -> None:
        f = tmp_path / "file.txt"
        f.write_text("x")
        with pytest.raises(NotADirectoryError):
            export_ro_crate(f, tmp_path / "out.zip")

    def test_creates_parent_dirs(self, minimal_capsule_dir: Path, tmp_path: Path) -> None:
        out = tmp_path / "deep" / "nested" / "out.zip"
        export_ro_crate(minimal_capsule_dir, out)
        assert out.exists()


class TestExportRoCrateCli:
    def test_default_output_path(self, minimal_capsule_dir: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(app, ["export-rocrate", str(minimal_capsule_dir)])
        assert result.exit_code == 0, result.output
        expected = minimal_capsule_dir.parent / f"{minimal_capsule_dir.name}.rocrate.zip"
        assert expected.exists()
        assert "RO-Crate" in result.output

    def test_explicit_output(self, minimal_capsule_dir: Path, tmp_path: Path) -> None:
        out = tmp_path / "my.zip"
        runner = CliRunner()
        result = runner.invoke(
            app, ["export-rocrate", str(minimal_capsule_dir), "--output", str(out)]
        )
        assert result.exit_code == 0, result.output
        assert out.exists()

    def test_missing_capsule_exits_1(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(app, ["export-rocrate", str(tmp_path / "ghost")])
        assert result.exit_code == 1
