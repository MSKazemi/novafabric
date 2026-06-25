# Copyright 2024 NovaFabric Contributors
# Apache-2.0 License
"""CLI tests for nova export-hipaa-proof."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def capsule_dir(tmp_path: Path) -> Path:
    """Minimal capsule directory with capsule.yaml."""
    d = tmp_path / "cap-hipaa-cli-001"
    d.mkdir()
    yaml.dump(
        {"run_id": "run-hipaa-cli-001", "schema_version": "1.0.0"},
        (d / "capsule.yaml").open("w"),
    )
    return d


@pytest.fixture()
def capsule_dir_with_redaction(capsule_dir: Path) -> Path:
    redaction = {
        "redacted_items": [
            {"pii_type": "EMAIL", "redacted": True},
            {"pii_type": "PHONE", "redacted": True},
        ],
    }
    (capsule_dir / "redaction_manifest.json").write_text(json.dumps(redaction))
    return capsule_dir


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestExportHIPAAProofCmd:
    def test_exits_0_with_explicit_output(
        self, capsule_dir: Path, tmp_path: Path
    ) -> None:
        output = tmp_path / "hipaa-proof.json"
        result = runner.invoke(
            app,
            ["export-hipaa-proof", str(capsule_dir), "--output", str(output)],
        )
        assert result.exit_code == 0, result.output
        assert output.exists()

    def test_written_json_contains_framework(
        self, capsule_dir: Path, tmp_path: Path
    ) -> None:
        output = tmp_path / "hipaa-proof.json"
        runner.invoke(
            app,
            ["export-hipaa-proof", str(capsule_dir), "--output", str(output)],
        )
        data = json.loads(output.read_text())
        assert "HIPAA" in data["framework"]
        assert "164.514" in data["framework"]

    def test_written_json_has_all_18_identifiers(
        self, capsule_dir: Path, tmp_path: Path
    ) -> None:
        output = tmp_path / "hipaa-proof.json"
        runner.invoke(
            app,
            ["export-hipaa-proof", str(capsule_dir), "--output", str(output)],
        )
        data = json.loads(output.read_text())
        assert len(data["identifiers"]) == 18

    def test_default_output_path_is_inside_capsule_dir(self, capsule_dir: Path) -> None:
        result = runner.invoke(
            app,
            ["export-hipaa-proof", str(capsule_dir)],
        )
        assert result.exit_code == 0, result.output
        default_output = capsule_dir / "hipaa-proof.json"
        assert default_output.exists()

    def test_help_includes_scope_and_examples(self) -> None:
        result = runner.invoke(app, ["export-hipaa-proof", "--help"])
        assert result.exit_code == 0
        combined = result.output.lower()
        assert "scope" in combined
        assert "example" in combined

    def test_help_includes_disclaimer_reference(self) -> None:
        result = runner.invoke(app, ["export-hipaa-proof", "--help"])
        assert result.exit_code == 0
        # Must mention limitation/disclaimer or legal nature in help
        assert "technical evidence" in result.output.lower() or "disclaimer" in result.output.lower() or "not legal" in result.output.lower() or "not a compliance" in result.output.lower()

    def test_redacted_categories_appear_in_output(
        self, capsule_dir_with_redaction: Path, tmp_path: Path
    ) -> None:
        output = tmp_path / "hipaa-proof.json"
        result = runner.invoke(
            app,
            ["export-hipaa-proof", str(capsule_dir_with_redaction), "--output", str(output)],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(output.read_text())
        by_id = {a["identifier"]: a for a in data["identifiers"]}
        assert by_id["email_addresses"]["status"] == "redacted"
        assert by_id["telephone_numbers"]["status"] == "redacted"

    def test_output_includes_proof_digest(
        self, capsule_dir: Path, tmp_path: Path
    ) -> None:
        output = tmp_path / "hipaa-proof.json"
        runner.invoke(
            app,
            ["export-hipaa-proof", str(capsule_dir), "--output", str(output)],
        )
        data = json.loads(output.read_text())
        assert data["proof_digest"].startswith("sha256:")

    def test_output_includes_legal_disclaimer(
        self, capsule_dir: Path, tmp_path: Path
    ) -> None:
        output = tmp_path / "hipaa-proof.json"
        runner.invoke(
            app,
            ["export-hipaa-proof", str(capsule_dir), "--output", str(output)],
        )
        data = json.loads(output.read_text())
        assert "NOT legal advice" in data["legal_disclaimer"]

    def test_short_option_o_works(self, capsule_dir: Path, tmp_path: Path) -> None:
        output = tmp_path / "hipaa-proof-short.json"
        result = runner.invoke(
            app,
            ["export-hipaa-proof", str(capsule_dir), "-o", str(output)],
        )
        assert result.exit_code == 0, result.output
        assert output.exists()
