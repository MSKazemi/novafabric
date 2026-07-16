"""ADR-0155 D1 — the ``nova forensics timeline`` CLI.

Read-only. Loads an incident's collected evidence (a JSON of event records + gaps) and prints the
deterministically ordered forensic timeline. Byte-identical across re-runs; missing evidence shows
as gaps, never an error. Exit 0 on render; 2 on missing/malformed input.
"""
from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()

_DOC = {
    "incident_id": "INC-1",
    "events": [
        {"ts": "2026-07-16T10:00:02Z", "source_capsule": "run-b", "seq": 0, "kind": "run"},
        {"ts": "2026-07-16T10:00:01Z", "source_capsule": "run-a", "seq": 0, "kind": "run"},
    ],
    "gaps": ["run-missing"],
}


def _write(tmp_path: Path, doc) -> Path:
    p = tmp_path / "ev.json"
    p.write_text(json.dumps(doc))
    return p


def test_timeline_renders_ordered_events_and_gaps(tmp_path):
    result = runner.invoke(app, ["forensics", "timeline", str(_write(tmp_path, _DOC))])
    assert result.exit_code == 0, result.output
    # earliest event (run-a) appears before the later one (run-b)
    assert result.output.index("run-a") < result.output.index("run-b")
    assert "run-missing" in result.output  # the gap is surfaced


def test_json_output_is_ordered_and_machine_readable(tmp_path):
    result = runner.invoke(
        app, ["forensics", "timeline", str(_write(tmp_path, _DOC)), "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["incident_id"] == "INC-1"
    assert [e["source_capsule"] for e in payload["events"]] == ["run-a", "run-b"]
    assert payload["gaps"] == ["run-missing"]


def test_missing_file_exits_two(tmp_path):
    result = runner.invoke(app, ["forensics", "timeline", str(tmp_path / "nope.json")])
    assert result.exit_code == 2


def test_malformed_json_exits_two(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{nope")
    result = runner.invoke(app, ["forensics", "timeline", str(p)])
    assert result.exit_code == 2
