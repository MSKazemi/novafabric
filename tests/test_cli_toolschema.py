"""ADR-0148 D2 / NF-165 — the ``nova toolschema impact`` CLI.

Read-only. Re-validates historical captured tool-call payloads against a new schema and reports which
runs break. Exit 0 whether or not runs break (it is evidence, not a gate); 2 on missing/malformed
input or a missing schema file.
"""
from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()

_NEW_SCHEMA = {
    "type": "object",
    "properties": {"query": {"type": "string"}},
    "required": ["query"],
}
_CALLS = {
    "tool_id": "mcp://acme/search",
    "tool_calls": [
        {"run_id": "run-1", "arguments": {"query": "x"}},
        {"run_id": "run-2", "arguments": {"q": "x"}},
        {"run_id": "run-3", "arguments": {}},
    ],
}


def _setup(tmp_path: Path) -> tuple[Path, Path]:
    schema = tmp_path / "new_schema.json"
    schema.write_text(json.dumps(_NEW_SCHEMA))
    calls = tmp_path / "calls.json"
    calls.write_text(json.dumps(_CALLS))
    return calls, schema


def test_reports_broken_runs(tmp_path):
    calls, schema = _setup(tmp_path)
    result = runner.invoke(
        app, ["toolschema", "impact", str(calls), "--new-schema", str(schema)]
    )
    assert result.exit_code == 0, result.output
    assert "run-2" in result.output
    assert "run-3" in result.output


def test_json_names_exact_broken_runs(tmp_path):
    calls, schema = _setup(tmp_path)
    result = runner.invoke(
        app,
        ["toolschema", "impact", str(calls), "--new-schema", str(schema), "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["tool_id"] == "mcp://acme/search"
    assert payload["checked"] == 3
    assert {b["run_id"] for b in payload["broken_run_ids"]} == {"run-2", "run-3"}
    assert payload["new_schema_digest"].startswith("sha256:")


def test_no_breaks_still_exit_zero(tmp_path):
    schema = tmp_path / "s.json"
    schema.write_text(json.dumps({"type": "object"}))  # permissive — nothing breaks
    calls = tmp_path / "c.json"
    calls.write_text(json.dumps(_CALLS))
    result = runner.invoke(
        app, ["toolschema", "impact", str(calls), "--new-schema", str(schema), "--json"]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["broken_run_ids"] == []


def test_missing_schema_file_exits_two(tmp_path):
    calls, _ = _setup(tmp_path)
    result = runner.invoke(
        app, ["toolschema", "impact", str(calls), "--new-schema", str(tmp_path / "nope.json")]
    )
    assert result.exit_code == 2


def test_missing_calls_file_exits_two(tmp_path):
    _, schema = _setup(tmp_path)
    result = runner.invoke(
        app,
        ["toolschema", "impact", str(tmp_path / "nope.json"), "--new-schema", str(schema)],
    )
    assert result.exit_code == 2


def test_malformed_calls_exits_two(tmp_path):
    _, schema = _setup(tmp_path)
    calls = tmp_path / "bad.json"
    calls.write_text("{nope")
    result = runner.invoke(
        app, ["toolschema", "impact", str(calls), "--new-schema", str(schema)]
    )
    assert result.exit_code == 2
