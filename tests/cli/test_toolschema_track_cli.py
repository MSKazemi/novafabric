"""NF-164 — the ``nova toolschema track`` CLI (ADR-0148 D2).

Classification is evidence, not a gate: exit ``0`` whatever the class, ``2`` only on bad input.
The class must survive the text rendering — a `breaking` change that prints as green `additive`
would be worse than no output at all.
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()

BASE = {
    "type": "object",
    "properties": {"query": {"type": "string"}, "top_k": {"type": "integer"}},
    "required": ["query"],
}


def _schemas(tmp_path: Path, to_schema: dict, from_schema: dict | None = None) -> list[str]:
    a, b = tmp_path / "from.json", tmp_path / "to.json"
    a.write_text(json.dumps(from_schema if from_schema is not None else BASE))
    b.write_text(json.dumps(to_schema))
    return ["--from-schema", str(a), "--to-schema", str(b)]


def _run(tmp_path: Path, to_schema: dict, *extra: str, from_schema: dict | None = None):
    return runner.invoke(
        app,
        ["toolschema", "track", "--tool", "mcp://acme/search"]
        + _schemas(tmp_path, to_schema, from_schema)
        + list(extra),
    )


def test_an_additive_change_exits_zero(tmp_path: Path) -> None:
    to_schema = {**BASE, "properties": {**BASE["properties"], "lang": {"type": "string"}}}
    result = _run(tmp_path, to_schema)
    assert result.exit_code == 0, result.output
    assert "additive" in result.output


def test_a_breaking_change_still_exits_zero(tmp_path: Path) -> None:
    """It classifies; it does not gate."""
    to_schema = {"type": "object", "properties": {"query": {"type": "string"}},
                 "required": ["query"]}
    result = _run(tmp_path, to_schema)
    assert result.exit_code == 0, result.output
    assert "breaking" in result.output


def test_the_text_output_names_the_class_and_the_differences(tmp_path: Path) -> None:
    to_schema = {"type": "object", "properties": {"query": {"type": "string"}},
                 "required": ["query"]}
    flat = " ".join(_run(tmp_path, to_schema).output.split())
    assert "breaking" in flat
    assert "removed: $.properties.top_k" in flat


def test_json_carries_the_full_record(tmp_path: Path) -> None:
    to_schema = {**BASE, "properties": {**BASE["properties"], "lang": {"type": "string"}}}
    result = _run(tmp_path, to_schema, "--json")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["change_class"] == "additive"
    assert payload["tool_id"] == "mcp://acme/search"
    assert payload["from_schema_digest"].startswith("sha256:")
    assert payload["diff"][0]["path"] == "$.properties.lang"


def test_an_unknown_class_reports_its_reason(tmp_path: Path) -> None:
    to_schema = {**BASE, "oneOf": [{"type": "object"}]}
    result = _run(tmp_path, to_schema, "--json")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["change_class"] == "unknown"
    assert "oneOf" in payload["reason"]


def test_truncation_is_visible_and_does_not_soften_the_class(tmp_path: Path) -> None:
    to_schema = {
        "type": "object",
        "properties": {f"opt_{i}": {"type": "string"} for i in range(10)},
        "required": [],
    }
    result = _run(tmp_path, to_schema, "--max-diff", "2", "--json")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["change_class"] == "breaking"
    assert payload["diff_truncated"] is True
    assert len(payload["diff"]) == 2
    assert payload["diff_total"] > 2


def test_a_missing_schema_exits_two(tmp_path: Path) -> None:
    result = runner.invoke(app, [
        "toolschema", "track", "--tool", "t",
        "--from-schema", str(tmp_path / "nope.json"),
        "--to-schema", str(tmp_path / "also-nope.json"),
    ])
    assert result.exit_code == 2


def test_a_malformed_schema_exits_two(tmp_path: Path) -> None:
    a, b = tmp_path / "from.json", tmp_path / "to.json"
    a.write_text("{not json")
    b.write_text(json.dumps(BASE))
    result = runner.invoke(app, [
        "toolschema", "track", "--tool", "t",
        "--from-schema", str(a), "--to-schema", str(b),
    ])
    assert result.exit_code == 2


def test_a_non_object_schema_exits_two(tmp_path: Path) -> None:
    a, b = tmp_path / "from.json", tmp_path / "to.json"
    a.write_text(json.dumps(BASE))
    b.write_text(json.dumps(["not", "a", "schema"]))
    result = runner.invoke(app, [
        "toolschema", "track", "--tool", "t",
        "--from-schema", str(a), "--to-schema", str(b),
    ])
    assert result.exit_code == 2
    assert "not a JSON Schema object" in result.output


def test_help_smoke() -> None:
    result = runner.invoke(app, ["toolschema", "track", "--help"])
    assert result.exit_code == 0
    assert "additive" in result.output.lower()
