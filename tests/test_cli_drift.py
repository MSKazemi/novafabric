"""ADR-0147 D2 / NF-151+NF-152 — the ``nova drift detect`` CLI.

Read-only. Computes an offline two-sample drift record from supplied baseline/window samples. Exit 0
on render (drift or not — it is evidence, not a gate); 2 on missing/malformed input or an unknown
statistic. This first slice takes the samples in the document; the capsule-window collector is a
documented follow-on.
"""
from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()


def _write(tmp_path: Path, doc) -> Path:
    p = tmp_path / "drift.json"
    p.write_text(json.dumps(doc))
    return p


_OUTPUT_DOC = {
    "kind": "output",
    "metric": "response-length-dist",
    "statistic": "psi",
    "baseline": [10, 11, 12, 13, 14],
    "window": [40, 41, 42, 43, 44],
    "threshold": 0.2,
    "window_meta": {"from": "2026-07-05", "to": "2026-07-12", "run_ids": ["r1"]},
    "baseline_id": "bl-2026Q2",
}


def test_output_drift_detected_exit_zero(tmp_path):
    result = runner.invoke(app, ["drift", "detect", str(_write(tmp_path, _OUTPUT_DOC))])
    assert result.exit_code == 0, result.output
    assert "drift" in result.output.lower()


def test_output_drift_json(tmp_path):
    result = runner.invoke(
        app, ["drift", "detect", str(_write(tmp_path, _OUTPUT_DOC)), "--json"]
    )
    assert result.exit_code == 0, result.output
    # stdout only: ADR-0147's honesty line rides on stderr precisely so that
    # `nova drift ... --json | jq` keeps working. Parsing the merged stream
    # would assert a contract the CLI does not offer.
    payload = json.loads(result.stdout)
    assert payload["kind"] == "output"
    assert payload["drifted"] is True
    assert payload["value"] > 0.2
    assert payload["window"]["run_ids"] == ["r1"]


def test_behavioral_tool_mix_json(tmp_path):
    doc = {
        "kind": "behavioral",
        "dimension": "tool-call-mix",
        "distance": "jensen-shannon",
        "baseline": {"search": 0.4, "db.query": 0.35, "email.send": 0.25},
        "window": {"search": 0.6, "db.query": 0.30, "email.send": 0.10},
        "threshold": 0.1,
    }
    result = runner.invoke(app, ["drift", "detect", str(_write(tmp_path, doc)), "--json"])
    assert result.exit_code == 0, result.output
    # stdout only: ADR-0147's honesty line rides on stderr precisely so that
    # `nova drift ... --json | jq` keeps working. Parsing the merged stream
    # would assert a contract the CLI does not offer.
    payload = json.loads(result.stdout)
    assert payload["kind"] == "behavioral"
    assert payload["dimension"] == "tool-call-mix"
    assert payload["value"] > 0.0


def test_unknown_statistic_exits_two(tmp_path):
    doc = dict(_OUTPUT_DOC)
    doc["statistic"] = "chi2"
    result = runner.invoke(app, ["drift", "detect", str(_write(tmp_path, doc))])
    assert result.exit_code == 2


def test_missing_file_exits_two(tmp_path):
    result = runner.invoke(app, ["drift", "detect", str(tmp_path / "nope.json")])
    assert result.exit_code == 2


def test_malformed_exits_two(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{nope")
    result = runner.invoke(app, ["drift", "detect", str(p)])
    assert result.exit_code == 2


# ── ADR-0147 D6 / NF-158 — nova drift silent-failure ───────────────────────────

_SF_DOC = {
    "threshold": 0.5,
    "runs": [
        {"run_id": "r1", "status": "success", "quality_signal": 0.2},
        {"run_id": "r2", "status": "success", "quality_signal": 0.9},
        {"run_id": "r3", "status": "failure", "quality_signal": 0.1},
    ],
}


def _write_named(tmp_path: Path, name: str, doc) -> Path:
    p = tmp_path / name
    p.write_text(json.dumps(doc))
    return p


def test_silent_failure_flags_and_exits_zero(tmp_path):
    result = runner.invoke(
        app, ["drift", "silent-failure", str(_write_named(tmp_path, "sf.json", _SF_DOC))]
    )
    assert result.exit_code == 0, result.output
    assert "silent-failure" in result.output.lower()


def test_silent_failure_json(tmp_path):
    result = runner.invoke(
        app,
        ["drift", "silent-failure", str(_write_named(tmp_path, "sf.json", _SF_DOC)), "--json"],
    )
    assert result.exit_code == 0, result.output
    # stdout only: ADR-0147's honesty line rides on stderr precisely so that
    # `nova drift ... --json | jq` keeps working. Parsing the merged stream
    # would assert a contract the CLI does not offer.
    payload = json.loads(result.stdout)
    assert payload["silent_failures"] == 1
    assert payload["total_reported_success"] == 2


def test_silent_failure_missing_quality_exits_two(tmp_path):
    doc = {"threshold": 0.5, "runs": [{"run_id": "r1", "status": "success"}]}
    result = runner.invoke(
        app, ["drift", "silent-failure", str(_write_named(tmp_path, "bad.json", doc))]
    )
    assert result.exit_code == 2


def test_silent_failure_runs_not_array_exits_two(tmp_path):
    doc = {"threshold": 0.5, "runs": "nope"}
    result = runner.invoke(
        app, ["drift", "silent-failure", str(_write_named(tmp_path, "bad.json", doc))]
    )
    assert result.exit_code == 2


# ── ADR-0147 D5 / NF-157 — nova drift root-cause ───────────────────────────────

_RC_DOC = {
    "baseline": [{"kind": "model", "ref": "m@1"}, {"kind": "prompt", "ref": "p@1"}],
    "drifted": [{"kind": "model", "ref": "m@2"}, {"kind": "prompt", "ref": "p@1"}],
}


def test_root_cause_renders_exit_zero(tmp_path):
    result = runner.invoke(
        app, ["drift", "root-cause", str(_write_named(tmp_path, "rc.json", _RC_DOC))]
    )
    assert result.exit_code == 0, result.output
    assert "correlation_only=True" in result.output


def test_root_cause_json(tmp_path):
    result = runner.invoke(
        app, ["drift", "root-cause", str(_write_named(tmp_path, "rc.json", _RC_DOC)), "--json"]
    )
    assert result.exit_code == 0, result.output
    # stdout only: ADR-0147's honesty line rides on stderr precisely so that
    # `nova drift ... --json | jq` keeps working. Parsing the merged stream
    # would assert a contract the CLI does not offer.
    payload = json.loads(result.stdout)
    assert payload["confidence"] == "sole_change"
    assert payload["correlation_only"] is True
    assert payload["changes"][0]["kind"] == "model"


def test_root_cause_baseline_not_array_exits_two(tmp_path):
    doc = {"baseline": "nope", "drifted": []}
    result = runner.invoke(
        app, ["drift", "root-cause", str(_write_named(tmp_path, "bad.json", doc))]
    )
    assert result.exit_code == 2
