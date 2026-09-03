"""nova assure-alarm — CLI surface for ADR-0147 D4 / NF-156."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()


def _doc(tmp_path: Path, baseline: list[int], window: list[int], **extra) -> Path:
    p = tmp_path / "window.json"
    p.write_text(json.dumps({"metric": "task_pass", "baseline": baseline,
                             "window": window, **extra}))
    return p


def test_a_sustained_regression_fires_and_exits_one(tmp_path: Path) -> None:
    doc = _doc(tmp_path, [1] * 40, [0] * 40)
    result = runner.invoke(app, ["assure-alarm", "check", "--window", str(doc)])

    assert result.exit_code == 1, result.output
    assert "ALARM" in result.output
    assert '"fired": true' in result.output.replace("'", '"').lower()


def test_a_healthy_window_exits_zero(tmp_path: Path) -> None:
    doc = _doc(tmp_path, [1] * 40, [1] * 40)
    result = runner.invoke(app, ["assure-alarm", "check", "--window", str(doc)])
    assert result.exit_code == 0, result.output


def test_a_single_dip_does_not_fire(tmp_path: Path) -> None:
    """The reason this reuses the SPRT rather than thresholding a delta."""
    doc = _doc(tmp_path, [1] * 40, [1] * 39 + [0])
    result = runner.invoke(app, ["assure-alarm", "check", "--window", str(doc)])
    assert result.exit_code == 0, result.output


def test_inconclusive_exits_zero_and_says_so(tmp_path: Path) -> None:
    doc = _doc(tmp_path, [1, 1, 1], [1, 0])
    result = runner.invoke(app, ["assure-alarm", "check", "--window", str(doc)])

    assert result.exit_code == 0, result.output
    assert "inconclusive" in result.output


def test_drift_flags_change_the_verdict(tmp_path: Path) -> None:
    """Without the flag the alarm is inverted, and every number still looks fine."""
    doc = _doc(tmp_path, [0] * 40, [1] * 40)

    raw = runner.invoke(app, ["assure-alarm", "check", "--window", str(doc)])
    flagged = runner.invoke(app, ["assure-alarm", "check", "--window", str(doc),
                                  "--drift-flags"])

    assert flagged.exit_code == 1, "an all-drifted window is a regression"
    assert raw.exit_code != flagged.exit_code, "the flag must change the outcome"


def test_sprt_parameters_are_tunable(tmp_path: Path) -> None:
    doc = _doc(tmp_path, [1] * 40, [0] * 40)
    result = runner.invoke(app, ["assure-alarm", "check", "--window", str(doc),
                                 "--p0", "0.9", "--p1", "0.5"])
    assert '"p0": 0.9' in result.output.replace("'", '"')


@pytest.mark.parametrize(
    ("doc", "why"),
    [
        ({"baseline": [1], "window": "nope"}, "window not an array"),
        ({"window": [1]}, "no baseline"),
        ({"baseline": [1, 2], "window": [1]}, "non-Bernoulli outcome"),
        ({"baseline": [], "window": [1]}, "empty baseline"),
    ],
)
def test_a_malformed_window_exits_two(doc: dict, why: str, tmp_path: Path) -> None:
    p = tmp_path / "w.json"
    p.write_text(json.dumps(doc))
    result = runner.invoke(app, ["assure-alarm", "check", "--window", str(p)])
    assert result.exit_code == 2, f"{why}: {result.output}"


def test_unparseable_json_exits_two(tmp_path: Path) -> None:
    p = tmp_path / "w.json"
    p.write_text("{not json")
    result = runner.invoke(app, ["assure-alarm", "check", "--window", str(p)])
    assert result.exit_code == 2


def test_a_missing_file_exits_two(tmp_path: Path) -> None:
    result = runner.invoke(app, ["assure-alarm", "check", "--window",
                                 str(tmp_path / "nope.json")])
    assert result.exit_code == 2


def test_it_never_uses_the_promote_gate_exit_code(tmp_path: Path) -> None:
    """ADR-0080's gate exits 3 on a regression; this alarm must not."""
    doc = _doc(tmp_path, [1] * 40, [0] * 40)
    result = runner.invoke(app, ["assure-alarm", "check", "--window", str(doc)])
    assert result.exit_code == 1, "an alarm exits 1, not the gate's 3"
