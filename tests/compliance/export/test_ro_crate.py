"""Tests for RO-Crate v1.1 export — library + CLI."""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from novafabric.cli.main import app
from novafabric.compliance.export.ro_crate import export_ro_crate


class TestRoCrateIdentifierIsTheRunId:
    """`identifier` must come from the manifest, not the directory name (ADR-0256).

    The exporter previously did `run_id = capsule_dir.name`. On a freshly captured
    capsule the directory *is* the run id, so the output was correct -- by
    coincidence -- and every existing test passed. It stops being correct the moment
    the directory is renamed, copied under another name, or extracted from an
    archive, which is exactly the situation RO-Crate's persistent `identifier` field
    exists to survive. Found by comparing the export against the capsule's own
    manifest instead of against a list of required keys.
    """

    @staticmethod
    def _root(out: Path) -> dict:
        with zipfile.ZipFile(out) as zf:
            meta = json.loads(zf.read("ro-crate-metadata.json"))
        return next(n for n in meta["@graph"] if n.get("@id") == "./")

    def test_identifier_survives_a_renamed_directory(
        self, minimal_capsule_dir: Path, tmp_path: Path
    ) -> None:
        renamed = minimal_capsule_dir.parent / "some-unrelated-folder-name"
        minimal_capsule_dir.rename(renamed)

        out = tmp_path / "renamed.rocrate.zip"
        export_ro_crate(renamed, out)
        root = self._root(out)

        assert root["identifier"] == "01HTEST000000000000000001"
        assert "some-unrelated-folder-name" not in root["identifier"]
        assert "01HTEST000000000000000001" in root["name"]

    def test_identifier_matches_manifest_run_id_normally(
        self, minimal_capsule_dir: Path, tmp_path: Path
    ) -> None:
        out = tmp_path / "normal.rocrate.zip"
        export_ro_crate(minimal_capsule_dir, out)
        assert self._root(out)["identifier"] == "01HTEST000000000000000001"

    def test_falls_back_to_directory_name_without_a_manifest(
        self, tmp_path: Path
    ) -> None:
        """A capsule with no readable manifest must still export, not raise."""
        bare = tmp_path / "bare-capsule"
        bare.mkdir()
        (bare / "trace.jsonl").touch()
        out = tmp_path / "bare.rocrate.zip"
        export_ro_crate(bare, out)
        assert self._root(out)["identifier"] == "bare-capsule"

    def test_damaged_manifest_falls_back_rather_than_raising(
        self, minimal_capsule_dir: Path, tmp_path: Path
    ) -> None:
        (minimal_capsule_dir / "capsule.yaml").write_text("{[not: valid: yaml")
        out = tmp_path / "damaged.rocrate.zip"
        export_ro_crate(minimal_capsule_dir, out)
        assert self._root(out)["identifier"] == minimal_capsule_dir.name


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
