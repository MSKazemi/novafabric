"""ADR-0166 D6 — the ``nova assure-case`` CLI (first slice).

A read-only command that loads an *assurance-case document* (a JSON bundle of the
argument graph plus optional currency ledger, conformance map, and defeaters) and reports:
structural validity, currency/drift, a conformance receipt, and open defeaters.

Invariants exercised here:
- exit 0 only when the case is structurally valid AND no defeater is open;
- a structurally invalid case OR any open defeater exits 1 (the argument is defeated);
- currency evaluation REQUIRES an explicit ``--as-of`` (never the system clock, per D2) —
  a document with a currency ledger but no ``--as-of`` fails closed (exit 2);
- ``--json`` emits a machine-readable report;
- the command never prints evidence bodies — only refs and digests.
"""
from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()


def _valid_case() -> dict:
    return {
        "case_id": "C1",
        "nodes": [
            {"id": "G1", "type": "goal", "statement": "System is safe",
             "supported_by": ["S1"]},
            {"id": "S1", "type": "solution", "statement": "Evidence of safety",
             "evidence_refs": [{"ref": "capsule://run-1", "digest": "a" * 64}]},
        ],
    }


def _write(tmp_path: Path, doc: dict) -> Path:
    p = tmp_path / "case.json"
    p.write_text(json.dumps(doc))
    return p


def test_valid_case_reports_valid_and_exits_zero(tmp_path):
    doc = {"case": _valid_case(), "resolvable_digests": ["a" * 64]}
    result = runner.invoke(app, ["assure-case", str(_write(tmp_path, doc))])
    assert result.exit_code == 0, result.output
    assert "G1" in result.output  # top goal surfaced
    assert "VALID" in result.output.upper()


def test_missing_file_exits_two(tmp_path):
    result = runner.invoke(app, ["assure-case", str(tmp_path / "nope.json")])
    assert result.exit_code == 2


def test_structurally_invalid_case_exits_one(tmp_path):
    # two top goals (no node points at either) → fatal structural error
    doc = {"case": {"case_id": "C2", "nodes": [
        {"id": "G1", "type": "goal", "statement": "a"},
        {"id": "G2", "type": "goal", "statement": "b"},
    ]}}
    result = runner.invoke(app, ["assure-case", str(_write(tmp_path, doc))])
    assert result.exit_code == 1, result.output


def test_open_defeater_defeats_and_exits_one(tmp_path):
    doc = {
        "case": _valid_case(),
        "resolvable_digests": ["a" * 64],
        "defeaters": [
            {"id": "D1", "target_node_id": "G1", "statement": "hazard H7 uncovered"},
        ],
    }
    result = runner.invoke(app, ["assure-case", str(_write(tmp_path, doc))])
    assert result.exit_code == 1, result.output
    assert "G1" in result.output  # the defeated node is named


def test_currency_without_as_of_fails_closed(tmp_path):
    doc = {
        "case": _valid_case(),
        "resolvable_digests": ["a" * 64],
        "currency": {"nodes": [
            {"node_id": "S1", "last_refreshed": "2026-01-01T00:00:00Z",
             "evidence_window": 86400},
        ]},
    }
    # no --as-of: must refuse rather than silently use the system clock
    result = runner.invoke(app, ["assure-case", str(_write(tmp_path, doc))])
    assert result.exit_code == 2, result.output
    assert "as-of" in result.output.lower()


def test_currency_overdue_with_as_of(tmp_path):
    doc = {
        "case": _valid_case(),
        "resolvable_digests": ["a" * 64],
        "currency": {"nodes": [
            {"node_id": "S1", "last_refreshed": "2026-01-01T00:00:00Z",
             "evidence_window": 86400},
        ]},
    }
    result = runner.invoke(
        app,
        ["assure-case", str(_write(tmp_path, doc)), "--as-of", "2026-06-01T00:00:00Z"],
    )
    # far past the 1-day window → overdue; drift is reported, exit stays 0 (valid, no defeater)
    assert result.exit_code == 0, result.output
    assert "overdue" in result.output.lower()


def test_malformed_defeater_exits_two(tmp_path):
    # a rebutted defeater with no resolving evidence is invalid → clean exit 2, not a traceback
    doc = {
        "case": _valid_case(),
        "resolvable_digests": ["a" * 64],
        "defeaters": [
            {"id": "D1", "target_node_id": "G1", "statement": "x", "state": "rebutted"},
        ],
    }
    result = runner.invoke(app, ["assure-case", str(_write(tmp_path, doc))])
    assert result.exit_code == 2, result.output


def test_json_output_is_machine_readable(tmp_path):
    doc = {
        "case": _valid_case(),
        "resolvable_digests": ["a" * 64],
        "conformance": {"entries": [
            {"node_id": "G1", "standard": "iso_iec_42001", "clause_id": "8.3",
             "claim_digest": "b" * 64},
        ]},
    }
    result = runner.invoke(
        app, ["assure-case", str(_write(tmp_path, doc)), "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["valid"] is True
    assert payload["top_goal_id"] == "G1"
    assert payload["open_defeaters"] == []
    assert "receipt" in payload  # conformance receipt embedded
