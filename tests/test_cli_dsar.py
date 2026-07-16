"""ADR-0161 D1 — the ``nova dsar assemble`` CLI.

Read-only. Loads a subject's collected DSAR records (keyed on the HMAC pseudonym) and renders the
deterministically assembled cross-capsule package. Byte-identical across re-runs; missing capsules
shown as gaps. Exit 0 on render; 2 on missing/malformed input.
"""
from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()

_DOC = {
    "subject_hmac": "hmac-abc",
    "records": [
        {"capsule_id": "run-c", "categories": ["email"], "purpose": "support"},
        {"capsule_id": "run-a", "categories": ["name"], "purpose": "billing"},
    ],
    "gaps": ["run-missing"],
}


def _write(tmp_path: Path, doc) -> Path:
    p = tmp_path / "dsar.json"
    p.write_text(json.dumps(doc))
    return p


def test_assemble_renders_ordered_capsules_and_gaps(tmp_path):
    result = runner.invoke(app, ["dsar", "assemble", str(_write(tmp_path, _DOC))])
    assert result.exit_code == 0, result.output
    assert result.output.index("run-a") < result.output.index("run-c")  # ordered by id
    assert "run-missing" in result.output  # gap surfaced
    assert "hmac-abc" in result.output


def test_json_output_is_ordered(tmp_path):
    result = runner.invoke(app, ["dsar", "assemble", str(_write(tmp_path, _DOC)), "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["subject_hmac"] == "hmac-abc"
    assert [c["capsule_id"] for c in payload["capsules"]] == ["run-a", "run-c"]
    assert payload["gaps"] == ["run-missing"]


def test_missing_file_exits_two(tmp_path):
    result = runner.invoke(app, ["dsar", "assemble", str(tmp_path / "nope.json")])
    assert result.exit_code == 2


def test_malformed_exits_two(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{nope")
    result = runner.invoke(app, ["dsar", "assemble", str(p)])
    assert result.exit_code == 2


# ── ADR-0161 D7 / NF-298 — nova dsar sla ───────────────────────────────────────

def _write_named(tmp_path: Path, name: str, doc) -> Path:
    p = tmp_path / name
    p.write_text(json.dumps(doc))
    return p


def test_sla_met_deadline_exit_zero(tmp_path):
    doc = {"request_open": "2026-06-01T12:00:00+00:00", "fulfilled_at": "2026-06-08T12:00:00+00:00"}
    result = runner.invoke(app, ["dsar", "sla", str(_write_named(tmp_path, "sla.json", doc))])
    assert result.exit_code == 0, result.output
    assert "met deadline" in result.output


def test_sla_json_past_deadline(tmp_path):
    doc = {"request_open": "2026-06-01T12:00:00+00:00", "fulfilled_at": "2026-07-15T12:00:00+00:00"}
    result = runner.invoke(
        app, ["dsar", "sla", str(_write_named(tmp_path, "sla.json", doc)), "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["met_deadline"] is False


def test_sla_missing_field_exits_two(tmp_path):
    doc = {"request_open": "2026-06-01T12:00:00+00:00"}
    result = runner.invoke(app, ["dsar", "sla", str(_write_named(tmp_path, "bad.json", doc))])
    assert result.exit_code == 2


def test_sla_fulfilled_before_open_exits_two(tmp_path):
    doc = {"request_open": "2026-06-10T12:00:00+00:00", "fulfilled_at": "2026-06-01T12:00:00+00:00"}
    result = runner.invoke(app, ["dsar", "sla", str(_write_named(tmp_path, "bad.json", doc))])
    assert result.exit_code == 2
