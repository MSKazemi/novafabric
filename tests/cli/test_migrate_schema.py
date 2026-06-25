"""Tests for ``nova migrate-schema`` command.

Covers:
- dry-run: prints what would change without writing any files
- backup: creates .v0.bak copies before overwriting originals
- schema_version added when absent
- event_log.jsonl renamed to model-calls.jsonl
- already-v1 capsule is skipped (not re-migrated)
"""
from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from novafabric.cli.main import app
from novafabric.cli.migrate_schema import migrate_capsule, migrate_capsule_dir

runner = CliRunner()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_capsule(
    parent: Path,
    name: str,
    *,
    schema_version: str | None = None,
    with_event_log: bool = False,
    with_model_calls: bool = False,
) -> Path:
    """Create a minimal capsule directory under *parent*."""
    cap = parent / name
    cap.mkdir(parents=True)
    manifest: dict = {
        "run_id": "01HXYZ1234567890123456789A",
        "status": "success",
    }
    if schema_version is not None:
        manifest["schema_version"] = schema_version
    (cap / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    if with_event_log:
        (cap / "event_log.jsonl").write_text("{}\n", encoding="utf-8")
    if with_model_calls:
        (cap / "model-calls.jsonl").write_text("{}\n", encoding="utf-8")
    return cap


# ---------------------------------------------------------------------------
# 1. dry-run: prints changes without writing
# ---------------------------------------------------------------------------


class TestDryRun:
    def test_dry_run_does_not_write(self, tmp_path: Path) -> None:
        """--dry-run must not modify any file on disk."""
        cap = _make_capsule(tmp_path, "cap-001", with_event_log=True)
        manifest_before = (cap / "manifest.json").read_text()

        result = runner.invoke(
            app,
            ["migrate-schema", "--capsule-dir", str(tmp_path), "--dry-run"],
        )

        assert result.exit_code == 0, result.output
        # manifest.json unchanged
        assert (cap / "manifest.json").read_text() == manifest_before
        # event_log.jsonl still present (not renamed)
        assert (cap / "event_log.jsonl").exists()
        assert not (cap / "model-calls.jsonl").exists()

    def test_dry_run_output_contains_would_migrate(self, tmp_path: Path) -> None:
        """--dry-run output must mention that a migration would occur."""
        _make_capsule(tmp_path, "cap-dry")
        result = runner.invoke(
            app,
            ["migrate-schema", "--capsule-dir", str(tmp_path), "--dry-run"],
        )
        assert result.exit_code == 0, result.output
        assert "would migrate" in result.output.lower() or "dry" in result.output.lower()


# ---------------------------------------------------------------------------
# 2. backup: creates .v0.bak files
# ---------------------------------------------------------------------------


class TestBackup:
    def test_backup_creates_manifest_bak(self, tmp_path: Path) -> None:
        """--backup must create manifest.json.v0.bak before overwriting."""
        _make_capsule(tmp_path, "cap-bak")
        result = runner.invoke(
            app,
            ["migrate-schema", "--capsule-dir", str(tmp_path), "--backup"],
        )
        assert result.exit_code == 0, result.output
        bak = tmp_path / "cap-bak" / "manifest.json.v0.bak"
        assert bak.exists(), "Expected manifest.json.v0.bak to be created"

    def test_backup_creates_event_log_bak(self, tmp_path: Path) -> None:
        """--backup must create event_log.jsonl.v0.bak when renaming."""
        _make_capsule(tmp_path, "cap-event-bak", with_event_log=True)
        result = runner.invoke(
            app,
            ["migrate-schema", "--capsule-dir", str(tmp_path), "--backup"],
        )
        assert result.exit_code == 0, result.output
        bak = tmp_path / "cap-event-bak" / "event_log.jsonl.v0.bak"
        assert bak.exists(), "Expected event_log.jsonl.v0.bak to be created"


# ---------------------------------------------------------------------------
# 3. schema_version added
# ---------------------------------------------------------------------------


class TestSchemaVersionAdded:
    def test_schema_version_absent_gets_set(self, tmp_path: Path) -> None:
        """manifest.json without schema_version must get schema_version=1.0.0."""
        cap = _make_capsule(tmp_path, "cap-no-ver")
        result = runner.invoke(
            app, ["migrate-schema", "--capsule-dir", str(tmp_path)]
        )
        assert result.exit_code == 0, result.output
        manifest = json.loads((cap / "manifest.json").read_text())
        assert manifest["schema_version"] == "1.0.0"

    def test_schema_version_zero_gets_updated(self, tmp_path: Path) -> None:
        """manifest.json with schema_version=0.1.0 must be upgraded to 1.0.0."""
        cap = _make_capsule(tmp_path, "cap-v0", schema_version="0.1.0")
        runner.invoke(app, ["migrate-schema", "--capsule-dir", str(tmp_path)])
        manifest = json.loads((cap / "manifest.json").read_text())
        assert manifest["schema_version"] == "1.0.0"

    def test_format_version_added_to_top_level(self, tmp_path: Path) -> None:
        """format_version key must be added when absent."""
        cap = _make_capsule(tmp_path, "cap-fv")
        runner.invoke(app, ["migrate-schema", "--capsule-dir", str(tmp_path)])
        manifest = json.loads((cap / "manifest.json").read_text())
        assert manifest.get("format_version") == "1"


# ---------------------------------------------------------------------------
# 4. event_log.jsonl renamed
# ---------------------------------------------------------------------------


class TestEventLogRenamed:
    def test_event_log_renamed_to_model_calls(self, tmp_path: Path) -> None:
        """event_log.jsonl must be renamed to model-calls.jsonl."""
        _make_capsule(tmp_path, "cap-rename", with_event_log=True)
        result = runner.invoke(
            app, ["migrate-schema", "--capsule-dir", str(tmp_path)]
        )
        assert result.exit_code == 0, result.output
        assert (tmp_path / "cap-rename" / "model-calls.jsonl").exists()
        assert not (tmp_path / "cap-rename" / "event_log.jsonl").exists()

    def test_event_log_not_renamed_if_model_calls_exists(self, tmp_path: Path) -> None:
        """Do not rename event_log.jsonl if model-calls.jsonl already exists."""
        _make_capsule(
            tmp_path, "cap-both", with_event_log=True, with_model_calls=True
        )
        runner.invoke(app, ["migrate-schema", "--capsule-dir", str(tmp_path)])
        # both files should still be present (no accidental delete / overwrite)
        assert (tmp_path / "cap-both" / "event_log.jsonl").exists()
        assert (tmp_path / "cap-both" / "model-calls.jsonl").exists()


# ---------------------------------------------------------------------------
# 5. already-v1 capsule skipped
# ---------------------------------------------------------------------------


class TestAlreadyV1Skipped:
    def test_v1_capsule_not_modified(self, tmp_path: Path) -> None:
        """A capsule already at schema_version 1.0.0 must not be touched."""
        cap = _make_capsule(tmp_path, "cap-v1", schema_version="1.0.0")
        manifest_before = (cap / "manifest.json").read_text()

        result = runner.invoke(
            app, ["migrate-schema", "--capsule-dir", str(tmp_path)]
        )
        assert result.exit_code == 0, result.output
        assert (cap / "manifest.json").read_text() == manifest_before

    def test_v1_capsule_reported_as_skipped(self, tmp_path: Path) -> None:
        """The output table must show 'skip' / 'already v1' for v1 capsules."""
        _make_capsule(tmp_path, "cap-v1-skip", schema_version="1.0.0")
        result = runner.invoke(
            app, ["migrate-schema", "--capsule-dir", str(tmp_path)]
        )
        assert result.exit_code == 0, result.output
        assert "already v1" in result.output

    def test_mixed_capsules_only_v0_migrated(self, tmp_path: Path) -> None:
        """Only v0 capsules are migrated; v1 capsules are left untouched."""
        cap_v0 = _make_capsule(tmp_path, "cap-mixed-v0")
        cap_v1 = _make_capsule(tmp_path, "cap-mixed-v1", schema_version="1.0.0")
        manifest_v1_before = (cap_v1 / "manifest.json").read_text()

        result = runner.invoke(
            app, ["migrate-schema", "--capsule-dir", str(tmp_path)]
        )
        assert result.exit_code == 0, result.output

        # v0 was migrated
        manifest_v0 = json.loads((cap_v0 / "manifest.json").read_text())
        assert manifest_v0["schema_version"] == "1.0.0"

        # v1 unchanged
        assert (cap_v1 / "manifest.json").read_text() == manifest_v1_before


# ---------------------------------------------------------------------------
# 6. Empty directory (no capsules found)
# ---------------------------------------------------------------------------


class TestEmptyDir:
    def test_empty_capsule_dir_exits_zero(self, tmp_path: Path) -> None:
        """An empty capsule directory should exit 0 with no errors."""
        result = runner.invoke(
            app, ["migrate-schema", "--capsule-dir", str(tmp_path)]
        )
        assert result.exit_code == 0, result.output

    def test_nonexistent_capsule_dir_exits_zero(self, tmp_path: Path) -> None:
        """A non-existent capsule directory should exit 0 with no errors."""
        result = runner.invoke(
            app,
            ["migrate-schema", "--capsule-dir", str(tmp_path / "does-not-exist")],
        )
        assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# Unit-level tests for migrate_capsule() helper
# ---------------------------------------------------------------------------


class TestMigrateCapsuleUnit:
    def test_skips_directory_without_manifest(self, tmp_path: Path) -> None:
        cap = tmp_path / "cap-no-manifest"
        cap.mkdir()
        result = migrate_capsule(cap, dry_run=False, backup=False)
        assert result.action == "skip"
        assert "no manifest.json" in result.result

    def test_returns_error_on_invalid_json(self, tmp_path: Path) -> None:
        cap = tmp_path / "cap-bad-json"
        cap.mkdir()
        (cap / "manifest.json").write_text("{invalid}", encoding="utf-8")
        result = migrate_capsule(cap, dry_run=False, backup=False)
        assert result.failed
        assert "invalid JSON" in result.result

    def test_migrate_capsule_dir_empty(self, tmp_path: Path) -> None:
        report = migrate_capsule_dir(tmp_path, dry_run=False, backup=False)
        assert report.results == []

    def test_migrate_capsule_dir_nonexistent(self, tmp_path: Path) -> None:
        report = migrate_capsule_dir(tmp_path / "ghost", dry_run=False, backup=False)
        assert report.results == []
