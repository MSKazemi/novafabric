# Copyright 2024 NovaFabric Contributors
# Apache-2.0 License
"""Tests for examiner-mode evidence exporters (BagIt, PCCP, ISO42001)."""

from __future__ import annotations

import hashlib
import json
import stat
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from novafabric.compliance.export.examiner import (
    BagItExporter,
    ISO42001Exporter,
    PCCPExporter,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

CAPSULE_ID = "run-test-abc123"
runner = CliRunner()


@pytest.fixture()
def capsule_dir(tmp_path: Path) -> Path:
    """Minimal capsule directory with capsule.yaml and a payload file."""
    d = tmp_path / CAPSULE_ID
    d.mkdir()
    manifest = {
        "run_id": CAPSULE_ID,
        "schema_version": "1.0.0",
        "asset_name": "my-agent",
        "model_id": "gpt-4o",
        "created_at": "2026-03-01T10:00:00Z",
        "eval_results": [
            {"suite_name": "nova-smoke-v1", "score": 0.95, "threshold": 0.90}
        ],
    }
    (d / "capsule.yaml").write_text(yaml.dump(manifest))
    (d / "capsule-manifest.json").write_bytes(json.dumps(manifest).encode())
    return d


@pytest.fixture()
def proposed_capsule_dir(tmp_path: Path) -> Path:
    """Second capsule directory for PCCP comparison."""
    d = tmp_path / "run-proposed-xyz"
    d.mkdir()
    manifest = {
        "run_id": "run-proposed-xyz",
        "schema_version": "1.0.0",
        "asset_name": "my-agent",
        "model_id": "gpt-4o-mini",
        "created_at": "2026-04-01T10:00:00Z",
        "eval_results": [
            {"suite_name": "nova-smoke-v1", "score": 0.97, "threshold": 0.90}
        ],
    }
    (d / "capsule.yaml").write_text(yaml.dump(manifest))
    return d


# ---------------------------------------------------------------------------
# BagItExporter tests
# ---------------------------------------------------------------------------


class TestBagItExporter:
    def test_creates_zip_file(self, tmp_path: Path, capsule_dir: Path) -> None:
        exporter = BagItExporter(capsule_id=CAPSULE_ID, data_dir=capsule_dir)
        bag_path = exporter.export(output_dir=tmp_path / "bags")

        assert bag_path.exists()
        assert bag_path.suffix == ".zip"
        assert CAPSULE_ID in bag_path.name

    def test_bagit_txt_present_with_correct_content(
        self, tmp_path: Path, capsule_dir: Path
    ) -> None:
        exporter = BagItExporter(capsule_id=CAPSULE_ID, data_dir=capsule_dir)
        bag_path = exporter.export(output_dir=tmp_path / "bags")

        with zipfile.ZipFile(bag_path) as zf:
            bagit_content = zf.read(f"{CAPSULE_ID}/bagit.txt").decode()

        assert "BagIt-Version: 1.0" in bagit_content
        assert "Tag-File-Character-Encoding: UTF-8" in bagit_content

    def test_bag_info_txt_present_with_metadata(
        self, tmp_path: Path, capsule_dir: Path
    ) -> None:
        exporter = BagItExporter(capsule_id=CAPSULE_ID, data_dir=capsule_dir)
        bag_path = exporter.export(output_dir=tmp_path / "bags", contact_email="test@example.com")

        with zipfile.ZipFile(bag_path) as zf:
            info_content = zf.read(f"{CAPSULE_ID}/bag-info.txt").decode()

        assert "Bag-Software-Agent: novafabric" in info_content
        assert f"External-Identifier: {CAPSULE_ID}" in info_content
        assert "Source-Organization: NovaFabric" in info_content
        assert "Bagging-Date:" in info_content
        assert "Contact-Email: test@example.com" in info_content

    def test_manifest_sha256_txt_present(
        self, tmp_path: Path, capsule_dir: Path
    ) -> None:
        exporter = BagItExporter(capsule_id=CAPSULE_ID, data_dir=capsule_dir)
        bag_path = exporter.export(output_dir=tmp_path / "bags")

        with zipfile.ZipFile(bag_path) as zf:
            names = zf.namelist()

        assert f"{CAPSULE_ID}/manifest-sha256.txt" in names

    def test_manifest_checksums_are_correct(
        self, tmp_path: Path, capsule_dir: Path
    ) -> None:
        exporter = BagItExporter(capsule_id=CAPSULE_ID, data_dir=capsule_dir)
        bag_path = exporter.export(output_dir=tmp_path / "bags")

        with zipfile.ZipFile(bag_path) as zf:
            manifest_content = zf.read(f"{CAPSULE_ID}/manifest-sha256.txt").decode()
            # Verify each checksum in the manifest against the actual data/ entry
            for line in manifest_content.strip().splitlines():
                if not line.strip():
                    continue
                checksum, filename = line.split("  ", 1)
                actual_bytes = zf.read(f"{CAPSULE_ID}/data/{filename.strip()}")
                actual_sha256 = hashlib.sha256(actual_bytes).hexdigest()
                assert actual_sha256 == checksum, (
                    f"Checksum mismatch for {filename}: "
                    f"expected {checksum}, got {actual_sha256}"
                )

    def test_tagmanifest_sha256_txt_present(
        self, tmp_path: Path, capsule_dir: Path
    ) -> None:
        exporter = BagItExporter(capsule_id=CAPSULE_ID, data_dir=capsule_dir)
        bag_path = exporter.export(output_dir=tmp_path / "bags")

        with zipfile.ZipFile(bag_path) as zf:
            names = zf.namelist()

        assert f"{CAPSULE_ID}/tagmanifest-sha256.txt" in names

    def test_verify_sh_present(self, tmp_path: Path, capsule_dir: Path) -> None:
        exporter = BagItExporter(capsule_id=CAPSULE_ID, data_dir=capsule_dir)
        bag_path = exporter.export(output_dir=tmp_path / "bags")

        with zipfile.ZipFile(bag_path) as zf:
            names = zf.namelist()
            verify_content = zf.read(f"{CAPSULE_ID}/verify.sh").decode()

        assert f"{CAPSULE_ID}/verify.sh" in names
        assert "sha256sum" in verify_content
        assert "manifest-sha256.txt" in verify_content

    def test_verify_sh_is_executable_in_zip(
        self, tmp_path: Path, capsule_dir: Path
    ) -> None:
        exporter = BagItExporter(capsule_id=CAPSULE_ID, data_dir=capsule_dir)
        bag_path = exporter.export(output_dir=tmp_path / "bags")

        with zipfile.ZipFile(bag_path) as zf:
            info = zf.getinfo(f"{CAPSULE_ID}/verify.sh")
            # External attributes store Unix permissions in the high 16 bits
            unix_perms = (info.external_attr >> 16) & 0o777
        # Must be executable by owner
        assert unix_perms & stat.S_IXUSR, f"verify.sh not executable: mode={oct(unix_perms)}"

    def test_data_directory_contains_payload(
        self, tmp_path: Path, capsule_dir: Path
    ) -> None:
        exporter = BagItExporter(capsule_id=CAPSULE_ID, data_dir=capsule_dir)
        bag_path = exporter.export(output_dir=tmp_path / "bags")

        with zipfile.ZipFile(bag_path) as zf:
            data_entries = [n for n in zf.namelist() if f"{CAPSULE_ID}/data/" in n]

        # capsule.yaml or capsule-manifest.json should appear in data/
        assert len(data_entries) >= 1

    def test_audit_report_excluded_when_flag_false(
        self, tmp_path: Path, capsule_dir: Path
    ) -> None:
        # Create an audit-report.json
        (capsule_dir / "audit-report.json").write_text('{"report": "ok"}')

        exporter = BagItExporter(capsule_id=CAPSULE_ID, data_dir=capsule_dir)
        bag_path = exporter.export(output_dir=tmp_path / "bags", include_audit_report=False)

        with zipfile.ZipFile(bag_path) as zf:
            names = zf.namelist()

        assert not any("audit-report.json" in n for n in names)

    def test_audit_report_included_when_present(
        self, tmp_path: Path, capsule_dir: Path
    ) -> None:
        (capsule_dir / "audit-report.json").write_text('{"report": "ok"}')

        exporter = BagItExporter(capsule_id=CAPSULE_ID, data_dir=capsule_dir)
        bag_path = exporter.export(output_dir=tmp_path / "bags", include_audit_report=True)

        with zipfile.ZipFile(bag_path) as zf:
            names = zf.namelist()

        assert any("audit-report.json" in n for n in names)

    def test_output_dir_created_if_absent(self, tmp_path: Path, capsule_dir: Path) -> None:
        new_dir = tmp_path / "deep" / "nested" / "bags"
        exporter = BagItExporter(capsule_id=CAPSULE_ID, data_dir=capsule_dir)
        bag_path = exporter.export(output_dir=new_dir)

        assert bag_path.exists()


# ---------------------------------------------------------------------------
# PCCPExporter tests
# ---------------------------------------------------------------------------


class TestPCCPExporter:
    def test_creates_json_file(
        self, tmp_path: Path, capsule_dir: Path, proposed_capsule_dir: Path
    ) -> None:
        exporter = PCCPExporter(
            baseline_capsule_id=CAPSULE_ID,
            proposed_capsule_id="run-proposed-xyz",
            data_dir=capsule_dir.parent,
        )
        out = exporter.export(output_path=tmp_path / "pccp.json")

        assert out.exists()
        data = json.loads(out.read_text())
        assert data["pccp_version"] == "1.0"

    def test_three_sections_present(
        self, tmp_path: Path, capsule_dir: Path, proposed_capsule_dir: Path
    ) -> None:
        exporter = PCCPExporter(
            baseline_capsule_id=CAPSULE_ID,
            proposed_capsule_id="run-proposed-xyz",
            data_dir=capsule_dir.parent,
        )
        out = exporter.export(output_path=tmp_path / "pccp.json")
        data = json.loads(out.read_text())

        assert "section_1_description_of_modifications" in data
        assert "section_2_modification_protocol" in data
        assert "section_3_impact_assessment" in data

    def test_section1_includes_capsule_ids(
        self, tmp_path: Path, capsule_dir: Path, proposed_capsule_dir: Path
    ) -> None:
        exporter = PCCPExporter(
            baseline_capsule_id=CAPSULE_ID,
            proposed_capsule_id="run-proposed-xyz",
            data_dir=capsule_dir.parent,
        )
        out = exporter.export(output_path=tmp_path / "pccp.json")
        data = json.loads(out.read_text())

        s1 = data["section_1_description_of_modifications"]
        assert s1["baseline_capsule"] == CAPSULE_ID
        assert s1["proposed_capsule"] == "run-proposed-xyz"
        assert "modification_summary" in s1

    def test_section2_has_testing_approach(
        self, tmp_path: Path, capsule_dir: Path, proposed_capsule_dir: Path
    ) -> None:
        exporter = PCCPExporter(
            baseline_capsule_id=CAPSULE_ID,
            proposed_capsule_id="run-proposed-xyz",
            data_dir=capsule_dir.parent,
        )
        out = exporter.export(output_path=tmp_path / "pccp.json")
        data = json.loads(out.read_text())

        s2 = data["section_2_modification_protocol"]
        assert "testing_approach" in s2
        assert "performance_metrics" in s2
        assert "acceptance_criteria" in s2

    def test_section3_has_risk_controls(
        self, tmp_path: Path, capsule_dir: Path, proposed_capsule_dir: Path
    ) -> None:
        exporter = PCCPExporter(
            baseline_capsule_id=CAPSULE_ID,
            proposed_capsule_id="run-proposed-xyz",
            data_dir=capsule_dir.parent,
        )
        out = exporter.export(output_path=tmp_path / "pccp.json")
        data = json.loads(out.read_text())

        s3 = data["section_3_impact_assessment"]
        assert "risk_controls" in s3
        assert isinstance(s3["risk_controls"], list)
        assert len(s3["risk_controls"]) >= 1

    def test_context_field_present(
        self, tmp_path: Path, capsule_dir: Path, proposed_capsule_dir: Path
    ) -> None:
        exporter = PCCPExporter(
            baseline_capsule_id=CAPSULE_ID,
            proposed_capsule_id="run-proposed-xyz",
            data_dir=capsule_dir.parent,
        )
        out = exporter.export(output_path=tmp_path / "pccp.json")
        data = json.loads(out.read_text())

        assert "@context" in data
        assert "pccp" in data["@context"]

    def test_generated_at_is_iso8601(
        self, tmp_path: Path, capsule_dir: Path, proposed_capsule_dir: Path
    ) -> None:
        exporter = PCCPExporter(
            baseline_capsule_id=CAPSULE_ID,
            proposed_capsule_id="run-proposed-xyz",
            data_dir=capsule_dir.parent,
        )
        out = exporter.export(output_path=tmp_path / "pccp.json")
        data = json.loads(out.read_text())

        # Should parse as ISO 8601
        datetime.fromisoformat(data["generated_at"].replace("Z", "+00:00"))

    def test_performance_metrics_from_eval_results(
        self, tmp_path: Path, capsule_dir: Path, proposed_capsule_dir: Path
    ) -> None:
        exporter = PCCPExporter(
            baseline_capsule_id=CAPSULE_ID,
            proposed_capsule_id="run-proposed-xyz",
            data_dir=capsule_dir.parent,
        )
        out = exporter.export(output_path=tmp_path / "pccp.json")
        data = json.loads(out.read_text())

        metrics = data["section_2_modification_protocol"]["performance_metrics"]
        assert isinstance(metrics, list)
        # proposed_capsule_dir has nova-smoke-v1 eval result
        assert any(m.get("suite") == "nova-smoke-v1" for m in metrics)


# ---------------------------------------------------------------------------
# ISO42001Exporter tests
# ---------------------------------------------------------------------------


class TestISO42001Exporter:
    def test_creates_zip_file(self, tmp_path: Path, capsule_dir: Path) -> None:
        exporter = ISO42001Exporter(
            data_dir=capsule_dir.parent,
            period_start=datetime(2026, 1, 1, tzinfo=timezone.utc),
            period_end=datetime(2026, 12, 31, tzinfo=timezone.utc),
        )
        out = exporter.export(output_path=tmp_path / "iso42001-package.zip")

        assert out.exists()
        assert zipfile.is_zipfile(out)

    def test_zip_contains_required_files(
        self, tmp_path: Path, capsule_dir: Path
    ) -> None:
        exporter = ISO42001Exporter(
            data_dir=capsule_dir.parent,
            period_start=datetime(2026, 1, 1, tzinfo=timezone.utc),
            period_end=datetime(2026, 12, 31, tzinfo=timezone.utc),
        )
        out = exporter.export(output_path=tmp_path / "iso42001-package.zip")

        with zipfile.ZipFile(out) as zf:
            names = zf.namelist()

        assert "iso42001-evidence.json" in names
        assert "control-narratives.json" in names
        assert "capsule-index.json" in names
        assert "README.txt" in names

    def test_evidence_json_has_correct_standard(
        self, tmp_path: Path, capsule_dir: Path
    ) -> None:
        exporter = ISO42001Exporter(
            data_dir=capsule_dir.parent,
            period_start=datetime(2026, 1, 1, tzinfo=timezone.utc),
            period_end=datetime(2026, 12, 31, tzinfo=timezone.utc),
        )
        out = exporter.export(output_path=tmp_path / "iso42001-package.zip")

        with zipfile.ZipFile(out) as zf:
            evidence = json.loads(zf.read("iso42001-evidence.json"))

        assert evidence["standard"] == "ISO/IEC 42001:2023"

    def test_controls_filter(self, tmp_path: Path, capsule_dir: Path) -> None:
        exporter = ISO42001Exporter(
            data_dir=capsule_dir.parent,
            period_start=datetime(2026, 1, 1, tzinfo=timezone.utc),
            period_end=datetime(2026, 12, 31, tzinfo=timezone.utc),
        )
        out = exporter.export(
            output_path=tmp_path / "iso42001-filtered.zip",
            controls=["6.1", "8.1"],
        )

        with zipfile.ZipFile(out) as zf:
            evidence = json.loads(zf.read("iso42001-evidence.json"))

        assert set(evidence["controls"].keys()) == {"6.1", "8.1"}

    def test_capsule_index_includes_period_capsule(
        self, tmp_path: Path, capsule_dir: Path
    ) -> None:
        exporter = ISO42001Exporter(
            data_dir=capsule_dir.parent,
            period_start=datetime(2026, 1, 1, tzinfo=timezone.utc),
            period_end=datetime(2026, 12, 31, tzinfo=timezone.utc),
        )
        out = exporter.export(output_path=tmp_path / "iso42001-package.zip")

        with zipfile.ZipFile(out) as zf:
            index = json.loads(zf.read("capsule-index.json"))

        # capsule_dir has created_at 2026-03-01 — within period
        run_ids = [entry["run_id"] for entry in index]
        assert CAPSULE_ID in run_ids

    def test_capsule_index_excludes_out_of_period(
        self, tmp_path: Path, capsule_dir: Path
    ) -> None:
        exporter = ISO42001Exporter(
            data_dir=capsule_dir.parent,
            period_start=datetime(2027, 1, 1, tzinfo=timezone.utc),
            period_end=datetime(2027, 12, 31, tzinfo=timezone.utc),
        )
        out = exporter.export(output_path=tmp_path / "iso42001-empty.zip")

        with zipfile.ZipFile(out) as zf:
            index = json.loads(zf.read("capsule-index.json"))

        # capsule created_at 2026-03-01 is outside 2027 window
        run_ids = [entry["run_id"] for entry in index]
        assert CAPSULE_ID not in run_ids

    def test_audit_period_in_evidence_json(
        self, tmp_path: Path, capsule_dir: Path
    ) -> None:
        exporter = ISO42001Exporter(
            data_dir=capsule_dir.parent,
            period_start=datetime(2026, 1, 1, tzinfo=timezone.utc),
            period_end=datetime(2026, 6, 30, tzinfo=timezone.utc),
        )
        out = exporter.export(output_path=tmp_path / "iso42001-package.zip")

        with zipfile.ZipFile(out) as zf:
            evidence = json.loads(zf.read("iso42001-evidence.json"))

        assert "audit_period" in evidence
        assert "2026-01-01" in evidence["audit_period"]["start"]
        assert "2026-06-30" in evidence["audit_period"]["end"]

    def test_empty_data_dir_produces_empty_index(self, tmp_path: Path) -> None:
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()

        exporter = ISO42001Exporter(
            data_dir=empty_dir,
            period_start=datetime(2026, 1, 1, tzinfo=timezone.utc),
            period_end=datetime(2026, 12, 31, tzinfo=timezone.utc),
        )
        out = exporter.export(output_path=tmp_path / "iso42001-empty.zip")

        with zipfile.ZipFile(out) as zf:
            index = json.loads(zf.read("capsule-index.json"))

        assert index == []


# ---------------------------------------------------------------------------
# CLI smoke tests
# ---------------------------------------------------------------------------


class TestCLISmoke:
    def test_help_bagit(self) -> None:
        from novafabric.cli.main import app

        result = runner.invoke(app, ["export-examiner", "bagit", "--help"])
        assert result.exit_code == 0
        assert "BagIt" in result.output or "bagit" in result.output.lower()

    def test_help_pccp(self) -> None:
        from novafabric.cli.main import app

        result = runner.invoke(app, ["export-examiner", "pccp", "--help"])
        assert result.exit_code == 0
        assert "PCCP" in result.output or "pccp" in result.output.lower()

    def test_help_iso42001(self) -> None:
        from novafabric.cli.main import app

        result = runner.invoke(app, ["export-examiner", "iso42001", "--help"])
        assert result.exit_code == 0
        assert "ISO" in result.output or "iso42001" in result.output.lower()

    def test_help_export_examiner(self) -> None:
        from novafabric.cli.main import app

        result = runner.invoke(app, ["export-examiner", "--help"])
        assert result.exit_code == 0

    def test_bagit_cli_creates_file(self, tmp_path: Path, capsule_dir: Path) -> None:
        from novafabric.cli.main import app

        result = runner.invoke(
            app,
            [
                "export-examiner",
                "bagit",
                CAPSULE_ID,
                "--output-dir", str(tmp_path / "bags"),
                "--data-dir", str(capsule_dir),
            ],
        )
        assert result.exit_code == 0, result.output
        assert (tmp_path / "bags" / f"{CAPSULE_ID}-bag.zip").exists()

    def test_pccp_cli_creates_file(
        self, tmp_path: Path, capsule_dir: Path, proposed_capsule_dir: Path
    ) -> None:
        from novafabric.cli.main import app

        out = tmp_path / "pccp.json"
        result = runner.invoke(
            app,
            [
                "export-examiner",
                "pccp",
                "--baseline", CAPSULE_ID,
                "--proposed", "run-proposed-xyz",
                "--output", str(out),
                "--data-dir", str(capsule_dir.parent),
            ],
        )
        assert result.exit_code == 0, result.output
        assert out.exists()

    def test_iso42001_cli_creates_file(self, tmp_path: Path, capsule_dir: Path) -> None:
        from novafabric.cli.main import app

        out = tmp_path / "iso42001-pkg.zip"
        result = runner.invoke(
            app,
            [
                "export-examiner",
                "iso42001",
                "--output", str(out),
                "--data-dir", str(capsule_dir.parent),
                "--period", "2026-01-01/2026-12-31",
            ],
        )
        assert result.exit_code == 0, result.output
        assert out.exists()

    def test_iso42001_cli_invalid_period(self, tmp_path: Path, capsule_dir: Path) -> None:
        from novafabric.cli.main import app

        result = runner.invoke(
            app,
            [
                "export-examiner",
                "iso42001",
                "--output", str(tmp_path / "out.zip"),
                "--data-dir", str(capsule_dir.parent),
                "--period", "not-a-valid-period",
            ],
        )
        assert result.exit_code != 0
