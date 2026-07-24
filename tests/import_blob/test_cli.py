"""CLI surface: ``nova import`` flags, exit codes, --json, receipts (ADR-0207)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from import_blob.helpers import (
    blob_path,
    export_capsules,
    make_capsule,
    read_manifest,
)
from novafabric.cli.main import app
from novafabric.evidence.signing import LocalSigner

runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolated_audit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep CLI-driven audit appends out of the developer's real audit log."""
    monkeypatch.setattr(
        "novafabric.import_blob.service.AUDIT_LOG_PATH",
        tmp_path / "cli-audit.jsonl",
    )


@pytest.fixture()
def export_dir(source_root: Path, tmp_path: Path, signer: LocalSigner) -> Path:
    capsules = [
        make_capsule(source_root, "run-a", content="A"),
        make_capsule(source_root, "run-b", content="B"),
    ]
    return export_capsules(capsules, tmp_path / "export", signer)


class TestImportCli:
    def test_help_smoke(self) -> None:
        result = runner.invoke(app, ["import", "--help"])
        assert result.exit_code == 0
        assert "--allow-unsigned" in result.output
        assert "--dry-run" in result.output
        assert "--public-key" in result.output

    def test_round_trip_with_json_report(
        self, export_dir: Path, store_root: Path, keys: tuple[Path, Path]
    ) -> None:
        result = runner.invoke(
            app,
            [
                "import",
                str(export_dir),
                "--public-key",
                str(keys[1]),
                "--capsule-dir",
                str(store_root),
                "--json",
            ],
        )
        assert result.exit_code == 0, result.output
        report = json.loads(result.stdout)
        assert report["verification"] == {
            "mode": "signed",
            "status": "VALID",
            "problems": [],
        }
        assert report["counts"]["imported"] == 2
        assert report["dry_run"] is False
        assert (store_root / "run-a").is_dir()
        assert (store_root / "run-b").is_dir()
        # Receipt persisted under $NOVAFABRIC_HOME/import-receipts/.
        from novafabric._paths import nova_home

        receipt_file = nova_home() / "import-receipts" / f"{report['import_id']}.json"
        assert receipt_file.is_file()

    def test_human_output_and_idempotent_rerun(
        self, export_dir: Path, store_root: Path, keys: tuple[Path, Path]
    ) -> None:
        args = [
            "import",
            str(export_dir),
            "--public-key",
            str(keys[1]),
            "--capsule-dir",
            str(store_root),
        ]
        first = runner.invoke(app, args)
        assert first.exit_code == 0, first.output
        assert "Imported 2 capsule(s)" in first.output

        second = runner.invoke(app, args)
        assert second.exit_code == 0, second.output
        assert "2 already present" in second.output

    def test_no_key_is_usage_error_exit_2(
        self, export_dir: Path, store_root: Path
    ) -> None:
        result = runner.invoke(
            app, ["import", str(export_dir), "--capsule-dir", str(store_root)]
        )
        assert result.exit_code == 2
        combined = result.output + result.stderr
        assert "--public-key" in combined
        assert "--allow-unsigned" in combined

    def test_unreadable_public_key_exit_2(
        self, export_dir: Path, store_root: Path, tmp_path: Path
    ) -> None:
        result = runner.invoke(
            app,
            [
                "import",
                str(export_dir),
                "--public-key",
                str(tmp_path / "missing.pem"),
                "--capsule-dir",
                str(store_root),
            ],
        )
        assert result.exit_code == 2

    def test_allow_unsigned_warns_loudly_and_records_mode(
        self, export_dir: Path, store_root: Path
    ) -> None:
        result = runner.invoke(
            app,
            [
                "import",
                str(export_dir),
                "--allow-unsigned",
                "--capsule-dir",
                str(store_root),
                "--json",
            ],
        )
        assert result.exit_code == 0, result.output
        combined = result.output + result.stderr
        assert "WARNING" in combined
        assert "authorship is unverified" in combined
        report = json.loads(result.stdout)
        assert report["verification"]["mode"] == "unsigned"

    def test_tampered_blob_exit_4_nothing_imported(
        self, export_dir: Path, store_root: Path, keys: tuple[Path, Path]
    ) -> None:
        raw = read_manifest(export_dir)
        target = blob_path(export_dir, raw["members"][0]["content_hash"])
        target.write_bytes(target.read_bytes() + b"tamper")
        result = runner.invoke(
            app,
            [
                "import",
                str(export_dir),
                "--public-key",
                str(keys[1]),
                "--capsule-dir",
                str(store_root),
            ],
        )
        assert result.exit_code == 4
        combined = result.output + result.stderr
        assert "Import refused" in combined
        assert "INCOMPLETE" in combined
        assert list(store_root.iterdir()) == []

    def test_dry_run_exit_codes_and_no_writes(
        self, export_dir: Path, store_root: Path, keys: tuple[Path, Path]
    ) -> None:
        result = runner.invoke(
            app,
            [
                "import",
                str(export_dir),
                "--public-key",
                str(keys[1]),
                "--capsule-dir",
                str(store_root),
                "--dry-run",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "DRY RUN" in result.output
        assert list(store_root.iterdir()) == []

    def test_collision_exit_5_lists_collision(
        self, export_dir: Path, store_root: Path, keys: tuple[Path, Path]
    ) -> None:
        args = [
            "import",
            str(export_dir),
            "--public-key",
            str(keys[1]),
            "--capsule-dir",
            str(store_root),
        ]
        assert runner.invoke(app, args).exit_code == 0
        (store_root / "run-a" / "outputs" / "stdout.txt").write_text("DIVERGED")
        result = runner.invoke(app, args)
        assert result.exit_code == 5
        assert "collision" in result.output
        assert "run-a" in result.output
