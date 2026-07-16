"""Slice A4 — `nova energy` CLI surface (ADR-0093)."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from novafabric.capsule.ulid_util import new_ulid
from novafabric.cli.energy import app

runner = CliRunner()


def _capsule(tmp: Path) -> Path:
    (tmp / "capsule.yaml").write_text(f"run_id: {new_ulid()}\n")
    (tmp / "model-calls.jsonl").write_text(
        json.dumps({"model_call_id": new_ulid(), "model": "gpt-4"}) + "\n"
    )
    return tmp


def test_probe_runs_and_reports(tmp_path):
    result = runner.invoke(app, ["probe", "--rapl-base", str(tmp_path / "none")])
    assert result.exit_code == 0
    assert "counter" in result.output.lower()


def test_attest_writes_receipts(tmp_path):
    capsule = _capsule(tmp_path)
    result = runner.invoke(
        app, ["attest", str(capsule), "--rapl-base", str(tmp_path / "none")]
    )
    assert result.exit_code == 0
    receipts = capsule / "energy-receipts.jsonl"
    assert receipts.exists()
    assert len([ln for ln in receipts.read_text().splitlines() if ln.strip()]) == 1


def test_verify_passes_on_honest_receipts(tmp_path):
    capsule = _capsule(tmp_path)
    runner.invoke(app, ["attest", str(capsule), "--rapl-base", str(tmp_path / "none")])
    result = runner.invoke(app, ["verify", str(capsule)])
    assert result.exit_code == 0


def test_verify_fails_on_forged_measured_receipt(tmp_path):
    capsule = _capsule(tmp_path)
    forged = {
        "schema_version": "0.1.0",
        "receipt_id": new_ulid(),
        "action_ref": {"kind": "model_call", "id": new_ulid()},
        "run_id": new_ulid(),
        "capsule_id": "capsule:x",
        "measured_joules": 999.0,
        "measurement_source": "nvml",
        "measurement_scope": "per_process",
        "confidence": "measured",
        "attribution_method": "direct_counter",
        "carbon_g_co2e": None,
        "grid_intensity_g_per_kwh": None,
        "carbon_accounting_mode": "unknown",
        "hardware": {"node_id": "n1", "counters_available": []},
        "payload_hash": "sha256:" + "0" * 64,
        "generated_at": "2026-06-19T00:00:00+00:00",
    }
    (capsule / "energy-receipts.jsonl").write_text(json.dumps(forged) + "\n")
    result = runner.invoke(app, ["verify", str(capsule)])
    assert result.exit_code != 0


def test_verify_out_of_enum_source_is_integrity_failure_not_crash(tmp_path):
    """A receipt with an unknown measurement_source must exit 3, not traceback."""
    capsule = _capsule(tmp_path)
    forged = {
        "schema_version": "0.1.0",
        "receipt_id": new_ulid(),
        "action_ref": {"kind": "model_call", "id": new_ulid()},
        "run_id": new_ulid(),
        "capsule_id": "capsule:x",
        "measured_joules": 1.0,
        "measurement_source": "rapl",  # not a valid enum member
        "measurement_scope": "per_process",
        "confidence": "measured",
        "attribution_method": "direct_counter",
        "carbon_g_co2e": None,
        "grid_intensity_g_per_kwh": None,
        "carbon_accounting_mode": "unknown",
        "hardware": {"node_id": "n1", "counters_available": []},
        "payload_hash": "sha256:" + "0" * 64,
        "generated_at": "2026-06-19T00:00:00+00:00",
    }
    (capsule / "energy-receipts.jsonl").write_text(json.dumps(forged) + "\n")
    result = runner.invoke(app, ["verify", str(capsule)])
    assert result.exit_code == 3
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "does not conform" in result.output


def test_verify_ok_when_no_receipts(tmp_path):
    capsule = _capsule(tmp_path)
    result = runner.invoke(app, ["verify", str(capsule)])
    assert result.exit_code == 0
    assert "no energy-receipts" in result.output.lower()


def test_report_json_format(tmp_path):
    capsule = _capsule(tmp_path)
    runner.invoke(app, ["attest", str(capsule), "--rapl-base", str(tmp_path / "none")])
    result = runner.invoke(app, ["report", str(capsule), "--format", "json"])
    assert result.exit_code == 0
    assert json.loads(result.output)[0]["confidence"] == "unknown"


def test_report_lists_receipts(tmp_path):
    capsule = _capsule(tmp_path)
    runner.invoke(app, ["attest", str(capsule), "--rapl-base", str(tmp_path / "none")])
    result = runner.invoke(app, ["report", str(capsule)])
    assert result.exit_code == 0
    assert "unavailable" in result.output.lower()
