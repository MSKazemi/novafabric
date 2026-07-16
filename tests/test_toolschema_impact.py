"""ADR-0148 D2 / NF-165 — tool-schema replay-impact analysis.

Given a new schema for a tool, re-validate **historical** captured tool-call payloads against it and
emit a ``schema_impact`` report naming exactly the runs that break (with per-run failing paths). It
**reuses the ADR-0128 validator** (``capture.schema_validation._check_target``) — it does not
reimplement schema validation.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from novafabric.supplychain.toolschema import impact as impact_mod
from novafabric.supplychain.toolschema.impact import (
    SchemaImpactReport,
    compute_schema_impact,
)

# A new schema that makes `query` required — past runs without it must break.
_NEW_SCHEMA = {
    "type": "object",
    "properties": {"query": {"type": "string"}},
    "required": ["query"],
}

_TOOL_CALLS = [
    {"run_id": "run-1", "arguments": {"query": "weather today"}},          # conforms
    {"run_id": "run-2", "arguments": {"q": "weather today"}},              # missing query → breaks
    {"run_id": "run-3", "arguments": {}},                                  # missing query → breaks
]


def _schema_file(tmp_path: Path) -> Path:
    p = tmp_path / "new_schema.json"
    p.write_text(json.dumps(_NEW_SCHEMA))
    return p


def test_reuses_adr0128_validator_by_import():
    from novafabric.capture import schema_validation
    # The impact analyzer binds the ADR-0128 validator core — it does not fork it.
    assert impact_mod._check_target is schema_validation._check_target


def test_names_exactly_the_broken_runs(tmp_path):
    report = compute_schema_impact(
        tool_id="mcp://acme/search",
        new_schema_path=_schema_file(tmp_path),
        tool_calls=_TOOL_CALLS,
    )
    assert isinstance(report, SchemaImpactReport)
    assert report.tool_id == "mcp://acme/search"
    assert report.checked == 3
    broken = {b.run_id for b in report.broken_run_ids}
    assert broken == {"run-2", "run-3"}  # exactly the runs missing `query`


def test_broken_run_carries_failing_paths(tmp_path):
    report = compute_schema_impact(
        tool_id="t", new_schema_path=_schema_file(tmp_path), tool_calls=_TOOL_CALLS
    )
    run2 = next(b for b in report.broken_run_ids if b.run_id == "run-2")
    assert run2.failing_paths  # at least one failing json path recorded


def test_new_schema_digest_is_sha256(tmp_path):
    report = compute_schema_impact(
        tool_id="t", new_schema_path=_schema_file(tmp_path), tool_calls=[]
    )
    assert report.new_schema_digest.startswith("sha256:")
    assert len(report.new_schema_digest.split(":", 1)[1]) == 64


def test_empty_tool_calls_checks_zero(tmp_path):
    report = compute_schema_impact(
        tool_id="t", new_schema_path=_schema_file(tmp_path), tool_calls=[]
    )
    assert report.checked == 0
    assert report.broken_run_ids == []


def test_records_without_target_are_not_checked(tmp_path):
    calls = [{"run_id": "r1"}, {"run_id": "r2", "arguments": {"query": "x"}}]
    report = compute_schema_impact(
        tool_id="t", new_schema_path=_schema_file(tmp_path), tool_calls=calls
    )
    assert report.checked == 1  # only r2 had `arguments`
    assert report.broken_run_ids == []


def test_missing_schema_file_raises(tmp_path):
    with pytest.raises(ValueError):
        compute_schema_impact(
            tool_id="t", new_schema_path=tmp_path / "nope.json", tool_calls=_TOOL_CALLS
        )


def test_report_has_no_verdict_or_gate_field():
    for forbidden in ("verdict", "passed", "gate", "promote", "approved"):
        assert forbidden not in SchemaImpactReport.model_fields
