"""nova assure-run — CLI surface for ADR-0147 D7 / NF-159."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()
RAN_AT = "2026-07-12T00:00:00Z"


def _record(out: Path):
    return runner.invoke(app, [
        "assure-run", "record", "--schedule", "nightly", "--ran-at", RAN_AT,
        "--cadence", "86400", "--baseline", "bl-golden",
        "--detector", "output-drift", "--alarms", "1", "--out", str(out),
    ])


def test_record_writes_an_attestation_with_a_derived_next_due(tmp_path: Path) -> None:
    out = tmp_path / "att.json"
    result = _record(out)

    assert result.exit_code == 0, result.output
    doc = json.loads(out.read_text())
    assert doc["schedule_id"] == "nightly"
    assert doc["next_due"] == "2026-07-13T00:00:00Z"
    assert doc["baselines_checked"] == ["bl-golden"]
    assert doc["alarms_fired"] == 1


def test_record_carries_the_assurance_honesty_line(tmp_path: Path) -> None:
    result = _record(tmp_path / "att.json")
    flat = " ".join(result.output.split())
    assert "remediate" in flat and "roll back" in flat


def test_check_exits_zero_when_on_time(tmp_path: Path) -> None:
    out = tmp_path / "att.json"
    assert _record(out).exit_code == 0

    result = runner.invoke(app, ["assure-run", "check", "--attestation", str(out),
                                 "--now", "2026-07-12T12:00:00Z"])
    assert result.exit_code == 0, result.output


def test_check_exits_one_when_a_run_was_missed(tmp_path: Path) -> None:
    """The miss left no record; the verdict comes from the previous success."""
    out = tmp_path / "att.json"
    assert _record(out).exit_code == 0

    result = runner.invoke(app, ["assure-run", "check", "--attestation", str(out),
                                 "--now", "2026-07-15T00:00:00Z"])
    assert result.exit_code == 1, result.output
    assert "overdue" in result.output


def test_a_bad_timestamp_exits_two(tmp_path: Path) -> None:
    result = runner.invoke(app, [
        "assure-run", "record", "--schedule", "s", "--ran-at", "yesterday",
        "--cadence", "3600",
    ])
    assert result.exit_code == 2


def test_a_non_positive_cadence_exits_two(tmp_path: Path) -> None:
    result = runner.invoke(app, [
        "assure-run", "record", "--schedule", "s", "--ran-at", RAN_AT,
        "--cadence", "0",
    ])
    assert result.exit_code == 2


def test_a_missing_attestation_exits_two(tmp_path: Path) -> None:
    result = runner.invoke(app, ["assure-run", "check", "--attestation",
                                 str(tmp_path / "nope.json"), "--now", RAN_AT])
    assert result.exit_code == 2
