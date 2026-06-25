# Copyright 2024 NovaFabric Contributors
# Apache-2.0 License
"""Tests for NIST AI RMF 1.0 quantitative risk reporter (cap-009)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml


@pytest.fixture()
def capsule_dir(tmp_path: Path) -> Path:
    d = tmp_path / "cap-nist-001"
    d.mkdir()
    manifest = {
        "schema_version": "1.0.0",
        "run_id": "run-nist-001",
        "asset_id": "asset-nist-001",
        "status": "production",
        "capture_level": "standard",
    }
    with open(d / "capsule.yaml", "w") as f:
        yaml.dump(manifest, f)
    return d


@pytest.fixture()
def capsule_dir_full(capsule_dir: Path) -> Path:
    """Capsule with all optional evidence files."""
    (capsule_dir / "evidence_bundle.json").write_text("{}")
    (capsule_dir / "audit_coverage.json").write_text(
        json.dumps({"coverage_fraction": 0.85, "controls_covered": 17, "controls_total": 20})
    )
    (capsule_dir / "eval_result.json").write_text(
        json.dumps({"suite_id": "smoke-v1", "metrics": [{"name": "accuracy", "value": 0.91}]})
    )
    # Use the real capture filenames/schema (hyphenated; RedactionManifest.entries).
    (capsule_dir / "tool-permission-events.jsonl").write_text(
        '{"event_type":"tool_permission","tool":"web_search","allowed":true}\n'
    )
    (capsule_dir / "redaction-manifest.json").write_text(
        json.dumps({"capsule_id": "01TEST", "entries": [
            {"field_path": "model_calls[0]", "detection_rule_id": "EMAIL",
             "legal_basis": "gdpr_art_17", "subject_id_hmac": "sha256:x",
             "redacted_at_utc": "2026-06-19T00:00:00Z"},
        ]})
    )
    return capsule_dir


class TestNISTAIRMFReporter:
    def test_build_report_minimal(self, capsule_dir: Path) -> None:
        from novafabric.compliance.export.nist_rmf import NISTAIRMFReporter

        report = NISTAIRMFReporter().build_report(capsule_dir)
        assert report.run_id == "run-nist-001"
        assert report.asset_id == "asset-nist-001"
        assert len(report.metrics) > 0

    def test_report_has_all_four_functions(self, capsule_dir_full: Path) -> None:
        from novafabric.compliance.export.nist_rmf import NISTAIRMFReporter

        report = NISTAIRMFReporter().build_report(capsule_dir_full)
        functions = {m.rmf_function for m in report.metrics}
        assert functions == {"GOVERN", "MAP", "MEASURE", "MANAGE"}

    def test_govern_score_from_audit_coverage(self, capsule_dir_full: Path) -> None:
        from novafabric.compliance.export.nist_rmf import NISTAIRMFReporter

        report = NISTAIRMFReporter().build_report(capsule_dir_full)
        assert report.govern_score is not None
        assert 0.0 <= report.govern_score <= 1.0
        # audit coverage = 0.85 → govern score should include it
        govern_metrics = [m for m in report.metrics if m.rmf_function == "GOVERN"]
        policy_metric = next(m for m in govern_metrics if m.metric_id == "GV-1.1")
        assert policy_metric.value == pytest.approx(0.85)

    def test_evidence_bundle_affects_govern_gv12(self, capsule_dir_full: Path) -> None:
        from novafabric.compliance.export.nist_rmf import NISTAIRMFReporter

        report = NISTAIRMFReporter().build_report(capsule_dir_full)
        gv12 = next(m for m in report.metrics if m.metric_id == "GV-1.2")
        assert gv12.value == 1.0
        assert gv12.compliant is True

    def test_pii_redaction_rate_manage(self, capsule_dir_full: Path) -> None:
        from novafabric.compliance.export.nist_rmf import NISTAIRMFReporter

        report = NISTAIRMFReporter().build_report(capsule_dir_full)
        mg22 = next(m for m in report.metrics if m.metric_id == "MG-2.2")
        assert mg22.value == pytest.approx(1.0)
        assert mg22.compliant is True

    def test_risk_level_low_for_high_score(self, capsule_dir_full: Path) -> None:
        from novafabric.compliance.export.nist_rmf import NISTAIRMFReporter

        report = NISTAIRMFReporter().build_report(capsule_dir_full)
        # With all evidence present and high scores, risk should be low or medium.
        assert report.risk_level in ("low", "medium")

    def test_risk_level_high_for_empty_capsule(self, capsule_dir: Path) -> None:
        from novafabric.compliance.export.nist_rmf import NISTAIRMFReporter

        report = NISTAIRMFReporter().build_report(capsule_dir)
        assert report.risk_level in ("high", "critical")

    def test_missing_evidence_populated(self, capsule_dir: Path) -> None:
        from novafabric.compliance.export.nist_rmf import NISTAIRMFReporter

        report = NISTAIRMFReporter().build_report(capsule_dir)
        assert len(report.missing_evidence) > 0
        assert "audit_coverage.json" in report.missing_evidence

    def test_overall_score_average_of_function_scores(self, capsule_dir_full: Path) -> None:
        from novafabric.compliance.export.nist_rmf import NISTAIRMFReporter

        report = NISTAIRMFReporter().build_report(capsule_dir_full)
        assert report.overall_score is not None
        scores = [
            s for s in [
                report.govern_score,
                report.map_score,
                report.measure_score,
                report.manage_score,
            ]
            if s is not None
        ]
        expected_avg = sum(scores) / len(scores)
        assert report.overall_score == pytest.approx(expected_avg, abs=1e-9)

    def test_export_json_round_trip(self, capsule_dir_full: Path, tmp_path: Path) -> None:
        from novafabric.compliance.export.nist_rmf import NISTAIRMFReporter

        reporter = NISTAIRMFReporter()
        report = reporter.build_report(capsule_dir_full)
        out = tmp_path / "nist_rmf.json"
        reporter.export_json(report, out)

        assert out.exists()
        data = json.loads(out.read_text())
        assert data["run_id"] == "run-nist-001"
        assert data["schema_version"] == "1.0.0"
        assert "metrics" in data
        assert len(data["metrics"]) > 0

    def test_capture_level_forensic_scores_higher(self, tmp_path: Path) -> None:
        from novafabric.compliance.export.nist_rmf import NISTAIRMFReporter

        for level, expected_min in [("forensic", 0.9), ("standard", 0.5), ("minimal", 0.1)]:
            d = tmp_path / f"cap-{level}"
            d.mkdir()
            with open(d / "capsule.yaml", "w") as f:
                yaml.dump({"run_id": f"run-{level}", "capture_level": level}, f)
            report = NISTAIRMFReporter().build_report(d)
            mp22 = next(m for m in report.metrics if m.metric_id == "MP-2.2")
            assert mp22.value is not None
            assert mp22.value >= expected_min, f"level={level} score={mp22.value}"
