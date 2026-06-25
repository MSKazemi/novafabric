from __future__ import annotations

import json
import sys
from pathlib import Path

from typer.testing import CliRunner

from novafabric.capture.orchestrator import CaptureOrchestrator
from novafabric.cli.main import app

runner = CliRunner()


def _make_capsule(tmp_path: Path, payload: str = "pass") -> Path:
    orch = CaptureOrchestrator(base_dir=tmp_path / "runs")
    result = orch.run(command=[sys.executable, "-c", payload])
    return result.capsule_dir


def _inject_finding(capsule_dir: Path, severity: str = "critical") -> None:
    proof_path = capsule_dir / "redaction-proof.json"
    proof = json.loads(proof_path.read_text())
    proof["findings"].append({
        "finding_id": "01HXAY7M5JZ8R7K4P9DPBYK2WX",
        "rule_id": "synthetic-test-rule",
        "rule_version": "0.1.0",
        "pack": "gitleaks-core-v0",
        "severity": severity,
        "target_kind": "trace",
        "target_ref": "trace.jsonl",
        "byte_offset": 0,
        "byte_length": 8,
        "match_hash": "sha256:" + "0" * 64,
        "redaction_strategy": "mask",
        "replacement": "[REDACTED:synthetic-test-rule]",
    })
    proof["findings_count"]["total"] = len(proof["findings"])
    proof["findings_count"]["by_severity"][severity] += 1
    proof_path.write_text(json.dumps(proof, indent=2))


def test_clean_capsule_exits_ok(tmp_path: Path) -> None:
    capsule_dir = _make_capsule(tmp_path)
    result = runner.invoke(app, ["scan-secrets", str(capsule_dir)])
    assert result.exit_code == 0
    assert "no findings" in result.output.lower() or "0 findings" in result.output


def test_capsule_with_findings_reports_summary(tmp_path: Path) -> None:
    capsule_dir = _make_capsule(tmp_path)
    _inject_finding(capsule_dir, severity="medium")
    result = runner.invoke(app, ["scan-secrets", str(capsule_dir)])
    assert result.exit_code == 0
    assert "synthetic-test-rule" in result.output
    assert "medium" in result.output.lower()


def test_fail_on_critical_blocks_critical_finding(tmp_path: Path) -> None:
    capsule_dir = _make_capsule(tmp_path)
    _inject_finding(capsule_dir, severity="critical")
    result = runner.invoke(app, ["scan-secrets", str(capsule_dir), "--fail-on", "critical"])
    assert result.exit_code == 2


def test_fail_on_high_passes_with_only_medium(tmp_path: Path) -> None:
    capsule_dir = _make_capsule(tmp_path)
    _inject_finding(capsule_dir, severity="medium")
    result = runner.invoke(app, ["scan-secrets", str(capsule_dir), "--fail-on", "high"])
    assert result.exit_code == 0


def test_json_output_is_parseable(tmp_path: Path) -> None:
    capsule_dir = _make_capsule(tmp_path)
    _inject_finding(capsule_dir, severity="high")
    result = runner.invoke(app, ["scan-secrets", str(capsule_dir), "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["findings_count"]["total"] >= 1
    assert payload["findings_count"]["by_severity"]["high"] >= 1


def test_missing_proof_errors(tmp_path: Path) -> None:
    capsule_dir = _make_capsule(tmp_path)
    (capsule_dir / "redaction-proof.json").unlink()
    result = runner.invoke(app, ["scan-secrets", str(capsule_dir)])
    assert result.exit_code == 1
    assert "redaction-proof.json" in result.output
