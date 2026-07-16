"""ADR-0173 (data slice) — the ``nova trust-radar`` CLI.

Read-only. Loads a JSON object of the seven verification guarantees and prints the fixed-axis
trust radar (rich or ``--json``). Exit code: ``1`` only on a ``critical`` verdict (a broken
seal-integrity anchor — signature/log-integrity); ``0`` for attested/partial/unsealed
(informational); ``2`` on a missing/malformed input.
"""
from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()

_FULL = {
    "signature_ok": True,
    "timestamp_ok": True,
    "log_integrity_ok": True,
    "redaction_coverage": 1.0,
    "secret_scan_clean": True,
    "policy_pass": True,
    "eval_gate_pass": True,
}


def _write(tmp_path: Path, flags: dict) -> Path:
    p = tmp_path / "verify.json"
    p.write_text(json.dumps(flags))
    return p


def test_attested_exits_zero(tmp_path):
    result = runner.invoke(app, ["trust-radar", str(_write(tmp_path, _FULL))])
    assert result.exit_code == 0, result.output
    assert "attested" in result.output.lower()
    assert "signature" in result.output.lower()


def test_critical_signature_exits_one(tmp_path):
    result = runner.invoke(
        app, ["trust-radar", str(_write(tmp_path, {**_FULL, "signature_ok": False}))]
    )
    assert result.exit_code == 1, result.output
    assert "critical" in result.output.lower()


def test_partial_exits_zero(tmp_path):
    result = runner.invoke(
        app, ["trust-radar", str(_write(tmp_path, {**_FULL, "timestamp_ok": False}))]
    )
    assert result.exit_code == 0, result.output
    assert "partial" in result.output.lower()


def test_json_output_is_machine_readable(tmp_path):
    result = runner.invoke(
        app, ["trust-radar", str(_write(tmp_path, _FULL)), "--json", "--capsule-id", "run-9"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["verdict"] == "attested"
    assert payload["capsule_id"] == "run-9"
    assert [a["key"] for a in payload["axes"]][0] == "signature"


def test_missing_file_exits_two(tmp_path):
    result = runner.invoke(app, ["trust-radar", str(tmp_path / "nope.json")])
    assert result.exit_code == 2


def test_malformed_json_exits_two(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json")
    result = runner.invoke(app, ["trust-radar", str(p)])
    assert result.exit_code == 2
