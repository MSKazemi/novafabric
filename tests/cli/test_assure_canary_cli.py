"""nova assure-canary — CLI surface for ADR-0147 D3 / NF-153 (evidence half)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()

STACK = {"model:gpt": "2026-07-18", "tool:search": "1.2.0"}


def _doc(tmp_path: Path, **over) -> Path:
    body = {"baseline_id": "bl-1", "ran_at": "2026-07-12T00:00:00Z",
            "stack": STACK, "equivalent": True, "drift_score": 0.0}
    body.update(over)
    p = tmp_path / "run.json"
    p.write_text(json.dumps(body))
    return p


def test_an_equivalent_canary_exits_zero(tmp_path: Path) -> None:
    result = runner.invoke(app, ["assure-canary", "record",
                                 "--run", str(_doc(tmp_path))])
    assert result.exit_code == 0, result.output
    assert '"alarm": false' in result.output.replace("'", '"').lower()


def test_a_non_equivalent_canary_alarms_and_exits_one(tmp_path: Path) -> None:
    doc = _doc(tmp_path, equivalent=False, drift_score=0.7)
    result = runner.invoke(app, ["assure-canary", "record", "--run", str(doc)])

    assert result.exit_code == 1, result.output
    assert "ALARM" in result.output


def test_a_changed_stack_is_surfaced(tmp_path: Path) -> None:
    """A difference may be the stack rather than the agent — say so."""
    doc = _doc(tmp_path, baseline_stack={**STACK, "model:gpt": "2026-06-01"})
    result = runner.invoke(app, ["assure-canary", "record", "--run", str(doc)])

    assert result.exit_code == 0, result.output
    assert "stack changed" in result.output


def test_an_unknown_baseline_stack_is_surfaced(tmp_path: Path) -> None:
    """'Not confirmed like-for-like' must not read as confirmed."""
    result = runner.invoke(app, ["assure-canary", "record",
                                 "--run", str(_doc(tmp_path))])
    assert "baseline stack unknown" in result.output


def test_a_matching_stack_is_not_flagged(tmp_path: Path) -> None:
    doc = _doc(tmp_path, baseline_stack=STACK)
    result = runner.invoke(app, ["assure-canary", "record", "--run", str(doc)])

    assert result.exit_code == 0, result.output
    assert "stack changed" not in result.output
    assert "baseline stack unknown" not in result.output


def test_the_record_is_written_to_out(tmp_path: Path) -> None:
    out = tmp_path / "record.json"
    result = runner.invoke(app, ["assure-canary", "record",
                                 "--run", str(_doc(tmp_path)), "--out", str(out)])

    assert result.exit_code == 0, result.output
    doc = json.loads(out.read_text())
    assert doc["baseline_id"] == "bl-1"
    assert doc["stack_fingerprint"].startswith("sha256:")


@pytest.mark.parametrize(
    ("over", "why"),
    [
        ({"stack": "nope"}, "stack not an object"),
        ({"equivalent": "yes"}, "verdict not a boolean"),
        ({"ran_at": "yesterday"}, "bad timestamp"),
        ({"baseline_id": "  "}, "empty baseline id"),
        ({"stack": {}}, "empty stack"),
        ({"baseline_stack": "nope"}, "baseline_stack not an object"),
    ],
)
def test_a_malformed_run_document_exits_two(
    over: dict, why: str, tmp_path: Path
) -> None:
    result = runner.invoke(app, ["assure-canary", "record",
                                 "--run", str(_doc(tmp_path, **over))])
    assert result.exit_code == 2, f"{why}: {result.output}"


def test_a_missing_verdict_exits_two(tmp_path: Path) -> None:
    p = tmp_path / "run.json"
    p.write_text(json.dumps({"baseline_id": "bl", "ran_at": "2026-07-12T00:00:00Z",
                             "stack": STACK}))
    result = runner.invoke(app, ["assure-canary", "record", "--run", str(p)])
    assert result.exit_code == 2


def test_unparseable_json_exits_two(tmp_path: Path) -> None:
    p = tmp_path / "run.json"
    p.write_text("{not json")
    result = runner.invoke(app, ["assure-canary", "record", "--run", str(p)])
    assert result.exit_code == 2


def test_a_missing_file_exits_two(tmp_path: Path) -> None:
    result = runner.invoke(app, ["assure-canary", "record", "--run",
                                 str(tmp_path / "nope.json")])
    assert result.exit_code == 2
