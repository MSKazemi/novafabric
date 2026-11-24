"""Tests for EU AI Act Art.12 compliance module and CLI (ADR-0076)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from novafabric.cli.main import app
from novafabric.compliance.euaiact import (
    EuAiActConfig,
    EuAiActExporter,
    infer_logging_event_types,
    is_within_retention,
)


def _make_capsule(root: Path, run_id: str, created_at: str, status: str = "success") -> Path:
    d = root / run_id
    d.mkdir(parents=True, exist_ok=True)
    meta = {
        "schema_version": "0.1.0",
        "run_id": run_id,
        "created_at": created_at,
        "status": status,
        "novafabric_version": "0.32.0",
        "model_call_count": 1,
        "tool_call_count": 0,
    }
    (d / "capsule.yaml").write_text(yaml.dump(meta))
    (d / "model-calls.jsonl").write_text(
        json.dumps({"model": "gpt-4o", "provider": "openai"}) + "\n"
    )
    return d


class TestEuAiActConfig:
    def test_defaults_inactive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NOVA_EUAIACT_HIGH_RISK", raising=False)
        monkeypatch.delenv("NOVA_EUAIACT_PROVIDER", raising=False)
        cfg = EuAiActConfig()
        assert not cfg.high_risk
        assert not cfg.provider_mode
        assert cfg.retention_months == 6

    def test_high_risk_env_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NOVA_EUAIACT_HIGH_RISK", "true")
        monkeypatch.delenv("NOVA_EUAIACT_PROVIDER", raising=False)
        cfg = EuAiActConfig()
        assert cfg.high_risk
        assert cfg.retention_months == 6

    def test_provider_mode_env_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NOVA_EUAIACT_HIGH_RISK", "true")
        monkeypatch.setenv("NOVA_EUAIACT_PROVIDER", "true")
        cfg = EuAiActConfig()
        assert cfg.provider_mode
        assert cfg.retention_months == 120

    def test_log_floor_is_six_months_in_both_modes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Art. 19(1) (providers) and Art. 26(6) (deployers): logs at least six months.
        # The 10-year Art. 18(1) period covers documentation, not logs (issue #63).
        monkeypatch.delenv("NOVA_EUAIACT_PROVIDER", raising=False)
        deployer = EuAiActConfig()
        assert deployer.log_retention_floor_months == 6
        assert deployer.documentation_retention_months is None
        monkeypatch.setenv("NOVA_EUAIACT_PROVIDER", "true")
        provider = EuAiActConfig()
        assert provider.log_retention_floor_months == 6
        assert provider.documentation_retention_months == 120

    def test_retention_constants(self) -> None:
        from novafabric.compliance import euaiact

        assert euaiact.LOG_RETENTION_FLOOR_MONTHS == 6
        assert euaiact.DOCUMENTATION_RETENTION_MONTHS == 120
        assert not hasattr(euaiact, "_PROVIDER_RETENTION_MONTHS")


class TestInferLoggingEventTypes:
    def test_interaction_timestamp_always_present(self, tmp_path: Path) -> None:
        d = _make_capsule(tmp_path, "run-001", "2024-01-01T00:00:00Z")
        meta = yaml.safe_load((d / "capsule.yaml").read_text())
        types = infer_logging_event_types(meta, d)
        assert "interaction_timestamp" in types

    def test_output_record_when_model_calls_present(self, tmp_path: Path) -> None:
        d = _make_capsule(tmp_path, "run-002", "2024-01-01T00:00:00Z")
        meta = yaml.safe_load((d / "capsule.yaml").read_text())
        types = infer_logging_event_types(meta, d)
        assert "output_record" in types

    def test_no_output_record_when_model_calls_absent(self, tmp_path: Path) -> None:
        d = _make_capsule(tmp_path, "run-003", "2024-01-01T00:00:00Z")
        (d / "model-calls.jsonl").unlink()
        meta = yaml.safe_load((d / "capsule.yaml").read_text())
        types = infer_logging_event_types(meta, d)
        assert "output_record" not in types

    def test_human_review_event_detected(self, tmp_path: Path) -> None:
        d = _make_capsule(tmp_path, "run-004", "2024-01-01T00:00:00Z")
        (d / "tool-calls.jsonl").write_text(
            json.dumps({"tool_name": "human_approval", "approved": True}) + "\n"
        )
        meta = yaml.safe_load((d / "capsule.yaml").read_text())
        types = infer_logging_event_types(meta, d)
        assert "human_review_event" in types

    def test_fallback_returns_list(self, tmp_path: Path) -> None:
        d = tmp_path / "empty"
        d.mkdir()
        (d / "capsule.yaml").write_text(yaml.dump({"run_id": "x"}))
        meta = {"run_id": "x"}
        types = infer_logging_event_types(meta, d)
        assert isinstance(types, list)
        assert len(types) >= 1


class TestEuAiActExporter:
    def test_returns_empty_for_nonexistent_dir(self, tmp_path: Path) -> None:
        exporter = EuAiActExporter(tmp_path / "ghost")
        assert exporter.export() == []

    def test_returns_all_capsules_no_filter(self, tmp_path: Path) -> None:
        caps = tmp_path / "capsules"
        _make_capsule(caps, "run-A", "2024-01-01T00:00:00Z")
        _make_capsule(caps, "run-B", "2024-06-01T00:00:00Z")
        exporter = EuAiActExporter(caps)
        records = exporter.export()
        assert len(records) == 2

    def test_date_filter_from(self, tmp_path: Path) -> None:
        caps = tmp_path / "capsules"
        _make_capsule(caps, "run-old", "2023-01-01T00:00:00Z")
        _make_capsule(caps, "run-new", "2024-06-01T00:00:00Z")
        exporter = EuAiActExporter(caps)
        from_dt = datetime(2024, 1, 1, tzinfo=timezone.utc)
        records = exporter.export(from_dt=from_dt)
        assert len(records) == 1
        assert records[0]["run_id"] == "run-new"

    def test_date_filter_to(self, tmp_path: Path) -> None:
        caps = tmp_path / "capsules"
        _make_capsule(caps, "run-early", "2023-01-01T00:00:00Z")
        _make_capsule(caps, "run-late", "2025-01-01T00:00:00Z")
        exporter = EuAiActExporter(caps)
        to_dt = datetime(2024, 1, 1, tzinfo=timezone.utc)
        records = exporter.export(to_dt=to_dt)
        assert len(records) == 1
        assert records[0]["run_id"] == "run-early"

    def test_record_has_required_fields(self, tmp_path: Path) -> None:
        caps = tmp_path / "capsules"
        _make_capsule(caps, "run-X", "2024-01-01T00:00:00Z")
        exporter = EuAiActExporter(caps)
        rec = exporter.export()[0]
        assert "run_id" in rec
        assert "logging_event_type" in rec
        assert "euaiact_compliance" in rec
        # Facts, never a verdict (review compliance-privacy-10).
        assert "art12_compliant" not in rec["euaiact_compliance"]
        assert "pii_redacted" not in rec["euaiact_compliance"]
        assert rec["euaiact_compliance"]["pii_redaction_manifest"] in ("present", "absent")

    def test_skips_directories_without_capsule_yaml(self, tmp_path: Path) -> None:
        caps = tmp_path / "capsules"
        caps.mkdir()
        (caps / "not-a-capsule").mkdir()
        exporter = EuAiActExporter(caps)
        assert exporter.export() == []


class TestIsWithinRetention:
    def test_recent_capsule_within_retention(self) -> None:
        # Relative to now: a fixed date here expired on 2026-10-01, six months after it.
        recent = datetime.now(tz=timezone.utc) - timedelta(weeks=7)
        assert is_within_retention(recent, 6) is True

    def test_old_capsule_outside_retention(self) -> None:
        old = datetime(2020, 1, 1, tzinfo=timezone.utc)
        assert is_within_retention(old, 6) is False


class TestEuAiActCli:
    @pytest.fixture(autouse=True)
    def _wide_console(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The command's module-level Rich console reads the terminal width, not the
        # CliRunner env, so an 80- or 60-column CI terminal wraps table cells and the
        # retention line. Pin a wide console so the assertions never depend on width.
        from rich.console import Console

        monkeypatch.setattr("novafabric.cli.euaiact.console", Console(width=300))

    def test_status_command(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NOVA_EUAIACT_HIGH_RISK", raising=False)
        monkeypatch.delenv("NOVA_EUAIACT_PROVIDER", raising=False)
        runner = CliRunner()
        result = runner.invoke(app, ["euaiact", "status"])
        assert result.exit_code == 0, result.output
        assert "NOVA_EUAIACT_HIGH_RISK" in result.output
        assert "Art. 19(1)" in result.output
        assert "Art.18 provider" not in result.output

    def test_status_provider_mode_labels_documentation_horizon(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NOVA_EUAIACT_PROVIDER", "true")
        result = CliRunner().invoke(app, ["euaiact", "status"], env={"COLUMNS": "200"})
        assert result.exit_code == 0, result.output
        assert "Art. 18(1) documentation, not logs" in result.output
        assert "6 months" in result.output and "120 months" in result.output

    def test_export_pretty_shows_the_log_floor(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        caps = tmp_path / "capsules"
        _make_capsule(caps, "run-P1", "2024-01-01T00:00:00Z")
        monkeypatch.setenv("NOVAFABRIC_CAPSULE_DIR", str(caps))
        monkeypatch.delenv("NOVA_EUAIACT_PROVIDER", raising=False)
        result = CliRunner().invoke(
            app, ["euaiact", "export", "--pretty"], env={"COLUMNS": "200"}
        )
        assert result.exit_code == 0, result.output
        flat = " ".join(result.output.split())
        assert "Log retention floor: 6 months (Art. 19(1) / Art. 26(6))" in flat
        assert "documentation horizon" not in flat
        monkeypatch.setenv("NOVA_EUAIACT_PROVIDER", "true")
        provider = CliRunner().invoke(
            app, ["euaiact", "export", "--pretty"], env={"COLUMNS": "200"}
        )
        pflat = " ".join(provider.output.split())
        assert "Log retention floor: 6 months" in pflat
        assert "documentation horizon applied: 120 months (Art. 18(1), provider mode)" in pflat

    def test_export_command_json_output(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        caps = tmp_path / "capsules"
        _make_capsule(caps, "run-E1", "2024-01-01T00:00:00Z")
        monkeypatch.setenv("NOVAFABRIC_CAPSULE_DIR", str(caps))
        monkeypatch.delenv("NOVA_EUAIACT_HIGH_RISK", raising=False)
        out = tmp_path / "art12.json"
        runner = CliRunner()
        result = runner.invoke(
            app, ["euaiact", "export", "--output", str(out)]
        )
        assert result.exit_code == 0, result.output
        records = json.loads(out.read_text())
        assert len(records) == 1
        assert records[0]["run_id"] == "run-E1"

    def test_export_command_date_filter(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        caps = tmp_path / "capsules"
        _make_capsule(caps, "run-old", "2023-01-01T00:00:00Z")
        _make_capsule(caps, "run-new", "2025-01-01T00:00:00Z")
        monkeypatch.setenv("NOVAFABRIC_CAPSULE_DIR", str(caps))
        monkeypatch.delenv("NOVA_EUAIACT_HIGH_RISK", raising=False)
        out = tmp_path / "filtered.json"
        runner = CliRunner()
        result = runner.invoke(
            app, ["euaiact", "export", "--from", "2024-01-01", "--output", str(out)]
        )
        assert result.exit_code == 0, result.output
        records = json.loads(out.read_text())
        assert len(records) == 1
        assert records[0]["run_id"] == "run-new"

    def test_export_invalid_from_date(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NOVAFABRIC_CAPSULE_DIR", str(tmp_path))
        runner = CliRunner()
        result = runner.invoke(app, ["euaiact", "export", "--from", "not-a-date"])
        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# The high-risk application date: Regulation (EU) 2026/1744 (Digital Omnibus on AI,
# OJ 24 July 2026) moved Annex III high-risk application to 2 December 2027
# (Regulation (EU) 2024/1689 Art. 113(c)). Art. 50 transparency kept 2 August 2026.
# ---------------------------------------------------------------------------

_ROOT = Path(__file__).resolve().parents[2]


def _euaiact_export_section() -> str:
    text = (_ROOT / "docs" / "cli-reference.md").read_text(encoding="utf-8")
    start = text.index("#### nova euaiact export")
    return text[start : text.index("\n---\n", start)]


class TestHighRiskDateIsStatedFromTheAmendedRegulation:
    def test_cli_reference_does_not_bind_art12_on_the_original_date(self) -> None:
        section = _euaiact_export_section()
        assert "high-risk AI systems: **2026-08-02**" not in section
        assert "Regulation (EU) 2026/1744" in section
        assert "2 December 2027" in section
        assert "Official Journal" in section

    def test_roadmap_does_not_bind_art12_on_the_original_date(self) -> None:
        for line in (_ROOT / "ROADMAP.md").read_text(encoding="utf-8").splitlines():
            tied = "Art.12 binding 2026-08-02" in line or line.startswith(
                "| **2026-08-02** | EU AI Act Art.12"
            )
            assert not tied, f"Art. 12 tied to the superseded date: {line}"

    def test_the_changelog_claims_no_eur_lex_check(self) -> None:
        text = " ".join((_ROOT / "CHANGELOG.md").read_text(encoding="utf-8").split())
        # A bool, not `assert ... not in text`: pytest's failure diff of a 1 MB string hangs.
        claims_a_check = "Checked against the EUR-Lex text" in text
        assert not claims_a_check


class TestRetentionDocs:
    def test_pii_erase_does_not_present_120_months_as_a_requirement(self) -> None:
        text = (_ROOT / "docs" / "cli-reference.md").read_text(encoding="utf-8")
        line = next(
            ln for ln in text.splitlines() if ln.startswith("- `--retention-months INT`")
        )
        assert "Art. 18(1)" in line and "documentation" in line
        assert "provider mode: `120`" not in line
        result = CliRunner().invoke(app, ["pii", "erase", "--help"], env={"COLUMNS": "200"})
        assert result.exit_code == 0, result.output
        flat = " ".join(result.output.split())
        assert "should use 120" not in flat
        assert "Art. 18(1)" in flat
