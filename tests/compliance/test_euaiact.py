"""Tests for EU AI Act Art.12 compliance module and CLI (ADR-0076)."""
from __future__ import annotations

import json
from datetime import datetime, timezone
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
        assert rec["euaiact_compliance"]["art12_compliant"] is True

    def test_skips_directories_without_capsule_yaml(self, tmp_path: Path) -> None:
        caps = tmp_path / "capsules"
        caps.mkdir()
        (caps / "not-a-capsule").mkdir()
        exporter = EuAiActExporter(caps)
        assert exporter.export() == []


class TestIsWithinRetention:
    def test_recent_capsule_within_retention(self) -> None:
        recent = datetime(2026, 4, 1, tzinfo=timezone.utc)  # ~7 weeks before 2026-05-20
        assert is_within_retention(recent, 6) is True

    def test_old_capsule_outside_retention(self) -> None:
        old = datetime(2020, 1, 1, tzinfo=timezone.utc)
        assert is_within_retention(old, 6) is False


class TestEuAiActCli:
    def test_status_command(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NOVA_EUAIACT_HIGH_RISK", raising=False)
        monkeypatch.delenv("NOVA_EUAIACT_PROVIDER", raising=False)
        runner = CliRunner()
        result = runner.invoke(app, ["euaiact", "status"])
        assert result.exit_code == 0, result.output
        assert "NOVA_EUAIACT_HIGH_RISK" in result.output

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
