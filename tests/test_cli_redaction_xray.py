"""ADR-0174 (data slice) — the ``nova redaction-xray`` CLI.

Read-only. Loads a JSON document describing a capsule's field-protection state and prints the
per-field state overlay + a coverage meter + per-state counts. The load-bearing invariant is
that **no field value is ever printed** — the command surfaces paths and states only.
"""
from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()


def _write(tmp_path: Path, doc) -> Path:
    p = tmp_path / "xray.json"
    p.write_text(json.dumps(doc))
    return p


def test_fields_list_reports_coverage_and_counts(tmp_path):
    doc = {"fields": [
        {"path": "p1", "state": "redacted"},
        {"path": "p2", "state": "secret_scrubbed"},
        {"path": "p3", "state": "clear"},
    ]}
    result = runner.invoke(app, ["redaction-xray", str(_write(tmp_path, doc))])
    assert result.exit_code == 0, result.output
    assert "redacted" in result.output.lower()
    assert "coverage" in result.output.lower()


def test_raw_findings_are_adapted(tmp_path):
    doc = {"findings": [
        {"target_ref": "env.yaml SECRET_KEY", "redaction_strategy": "drop",
         "match_hash": "cafe", "replacement": ""},
    ]}
    result = runner.invoke(app, ["redaction-xray", str(_write(tmp_path, doc))])
    assert result.exit_code == 0, result.output
    assert "SECRET_KEY" in result.output  # the path is shown
    assert "secret_scrubbed" in result.output.lower()


def test_values_are_never_printed(tmp_path):
    # a caller hands us a record carrying a raw secret value; it must not appear in output
    doc = {"fields": [
        {"path": "api_key", "state": "redacted", "value": "sk-live-DEADBEEF"},
    ]}
    result = runner.invoke(app, ["redaction-xray", str(_write(tmp_path, doc))])
    assert result.exit_code == 0, result.output
    assert "sk-live-DEADBEEF" not in result.output


def test_json_output_is_machine_readable(tmp_path):
    doc = {"fields": [{"path": "p1", "state": "redacted"}]}
    result = runner.invoke(
        app, ["redaction-xray", str(_write(tmp_path, doc)), "--json", "--capsule-id", "run-7"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["capsule_id"] == "run-7"
    assert payload["coverage"] == 1.0
    assert payload["fields"][0] == {"path": "p1", "state": "redacted"}


def test_missing_file_exits_two(tmp_path):
    result = runner.invoke(app, ["redaction-xray", str(tmp_path / "nope.json")])
    assert result.exit_code == 2


def test_malformed_json_exits_two(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{nope")
    result = runner.invoke(app, ["redaction-xray", str(p)])
    assert result.exit_code == 2
