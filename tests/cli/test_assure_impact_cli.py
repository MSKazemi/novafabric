"""nova assure-impact — CLI surface for ADR-0147 D3 / NF-154."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()

CORPUS = {
    "from_model": "gpt-a",
    "to_model": "gpt-b",
    "runs": [
        {"baseline_id": "bl-1", "equivalent": True, "distance": 0.0,
         "cost_before": {"amount_minor": 100, "currency": "EUR"},
         "cost_after": {"amount_minor": 90, "currency": "EUR"},
         "tokens_before": 1000, "tokens_after": 900},
        {"baseline_id": "bl-2", "equivalent": False, "distance": 0.6},
        {"baseline_id": "bl-3", "equivalent": None},
    ],
}


@pytest.fixture()
def corpus(tmp_path: Path) -> Path:
    p = tmp_path / "corpus.json"
    p.write_text(json.dumps(CORPUS))
    return p


def test_report_aggregates_the_corpus(corpus: Path, tmp_path: Path) -> None:
    out = tmp_path / "report.json"
    result = runner.invoke(app, ["assure-impact", "report", "--corpus", str(corpus),
                                 "--out", str(out)])

    assert result.exit_code == 0, result.output
    doc = json.loads(out.read_text())
    assert doc["n"] == 3
    assert doc["equivalent"] == 1
    assert doc["regressed"] == 1
    assert doc["inconclusive"] == 1
    assert doc["worst_regressions"][0]["baseline_id"] == "bl-2"


def test_regressions_do_not_make_the_command_fail(corpus: Path) -> None:
    """Exiting non-zero on a regression would be the adoption decision the ADR forbids."""
    result = runner.invoke(app, ["assure-impact", "report", "--corpus", str(corpus)])
    assert result.exit_code == 0, result.output


def test_it_reports_incomplete_coverage_rather_than_hiding_it(corpus: Path) -> None:
    """Two runs carry no cost data; a delta over 1 of 3 must say so."""
    result = runner.invoke(app, ["assure-impact", "report", "--corpus", str(corpus)])

    flat = " ".join(result.output.split())
    assert "inconclusive" in flat
    assert "carried no data" in flat


def test_mixed_currencies_exit_two(tmp_path: Path) -> None:
    doc = {
        "from_model": "a", "to_model": "b",
        "runs": [
            {"baseline_id": "bl-1", "equivalent": True,
             "cost_before": {"amount_minor": 1, "currency": "EUR"},
             "cost_after": {"amount_minor": 1, "currency": "EUR"}},
            {"baseline_id": "bl-2", "equivalent": True,
             "cost_before": {"amount_minor": 1, "currency": "JPY"},
             "cost_after": {"amount_minor": 1, "currency": "JPY"}},
        ],
    }
    p = tmp_path / "c.json"
    p.write_text(json.dumps(doc))

    result = runner.invoke(app, ["assure-impact", "report", "--corpus", str(p)])
    assert result.exit_code == 2, result.output


@pytest.mark.parametrize(
    ("doc", "why"),
    [
        ({"to_model": "b", "runs": []}, "no from_model"),
        ({"from_model": "a", "runs": []}, "no to_model"),
        ({"from_model": "a", "to_model": "b"}, "no runs"),
        ({"from_model": "a", "to_model": "b", "runs": "nope"}, "runs not an array"),
        ({"from_model": "a", "to_model": "b", "runs": []}, "empty corpus"),
    ],
)
def test_a_malformed_corpus_exits_two(doc: dict, why: str, tmp_path: Path) -> None:
    p = tmp_path / "c.json"
    p.write_text(json.dumps(doc))

    result = runner.invoke(app, ["assure-impact", "report", "--corpus", str(p)])
    assert result.exit_code == 2, f"{why}: {result.output}"


def test_unparseable_json_exits_two(tmp_path: Path) -> None:
    p = tmp_path / "c.json"
    p.write_text("{not json")
    result = runner.invoke(app, ["assure-impact", "report", "--corpus", str(p)])
    assert result.exit_code == 2


def test_a_missing_file_exits_two(tmp_path: Path) -> None:
    result = runner.invoke(app, ["assure-impact", "report", "--corpus",
                                 str(tmp_path / "nope.json")])
    assert result.exit_code == 2


def test_stdout_mode_prints_the_report(corpus: Path) -> None:
    result = runner.invoke(app, ["assure-impact", "report", "--corpus", str(corpus)])
    assert result.exit_code == 0
    assert "worst_regressions" in result.output
