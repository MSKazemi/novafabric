"""ADR-0147 D5 / NF-155 — the ``nova drift fingerprint`` CLI.

Read-only. Fingerprints a run's behaviour and, when a baseline is supplied, measures the distance
to it. A shift is evidence, not a gate: exit ``0`` whether or not it is flagged, ``2`` only on bad
input. The ADR-0147 I-4 honesty line must appear on both the text and the ``--json`` path.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from novafabric.assure._honesty import HONESTY_LINE
from novafabric.cli.main import app

runner = CliRunner()

_CALLS = [
    {"name": "search", "arguments": {"q": "novafabric"}},
    {"name": "read", "arguments": {"path": "a.txt"}},
    {"name": "write", "arguments": {"path": "out.txt"}},
]


def _write(tmp_path: Path, doc: Any) -> Path:
    p = tmp_path / "fingerprint.json"
    p.write_text(json.dumps(doc))
    return p


def _invoke(tmp_path: Path, doc: Any, *args: str):
    return runner.invoke(app, ["drift", "fingerprint", str(_write(tmp_path, doc)), *args])


def test_a_run_alone_is_fingerprinted_without_a_comparison(tmp_path: Path) -> None:
    result = _invoke(tmp_path, {"run": {"run_id": "r1", "calls": _CALLS}})
    assert result.exit_code == 0, result.output
    assert "sha256:" in result.output
    assert "vs baseline" not in result.output


def test_an_unshifted_comparison_exits_zero(tmp_path: Path) -> None:
    doc = {
        "run": {"run_id": "r1", "calls": _CALLS},
        "baseline": {"run_id": "b1", "calls": _CALLS},
        "threshold": 0.2,
    }
    result = _invoke(tmp_path, doc)
    assert result.exit_code == 0, result.output
    assert "stable" in result.output


def test_a_shift_is_reported_but_still_exits_zero(tmp_path: Path) -> None:
    """A detector observation must not become a gate by way of an exit code."""
    doc = {
        "run": {"run_id": "r1", "calls": [{"name": "deploy", "arguments": {}}]},
        "baseline": {"run_id": "b1", "calls": _CALLS},
        "threshold": 0.2,
    }
    result = _invoke(tmp_path, doc)
    assert result.exit_code == 0, result.output
    assert "SHIFTED" in result.output


def test_json_carries_the_comparison_and_its_components(tmp_path: Path) -> None:
    doc = {
        "run": {"run_id": "r1", "calls": _CALLS, "scores": [0.4]},
        "baseline": {"run_id": "b1", "calls": _CALLS, "scores": [0.9]},
        "threshold": 0.1,
    }
    result = _invoke(tmp_path, doc, "--json")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["shifted"] is True
    assert {c["component"] for c in payload["components"]} == {
        "trajectory",
        "tool-mix",
        "score-profile",
    }


def test_an_incomparable_pair_reports_unknown_not_unchanged(tmp_path: Path) -> None:
    doc = {
        "run": {"run_id": "r1", "calls": [], "scores": [0.9]},
        "baseline": {"run_id": "b1", "calls": _CALLS},
        "threshold": 0.1,
    }
    result = _invoke(tmp_path, doc, "--json")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["distance"] is None
    assert "shifted" not in payload or payload["shifted"] is None


def test_the_honesty_line_is_printed_on_both_paths(tmp_path: Path) -> None:
    doc = {"run": {"run_id": "r1", "calls": _CALLS}}
    text = _invoke(tmp_path, doc)
    assert HONESTY_LINE.split(".")[0] in text.output.replace("\n", " ")
    as_json = _invoke(tmp_path, doc, "--json")
    assert HONESTY_LINE.split(".")[0] in as_json.output.replace("\n", " ")
    json.loads(as_json.stdout)  # the line must not pollute stdout


def test_a_run_with_nothing_observable_is_refused(tmp_path: Path) -> None:
    result = _invoke(tmp_path, {"run": {"run_id": "r1", "calls": []}})
    assert result.exit_code == 2
    assert "no observable behaviour" in result.output


def test_an_out_of_range_score_is_refused(tmp_path: Path) -> None:
    doc = {"run": {"run_id": "r1", "calls": _CALLS, "scores": [4.0]}}
    result = _invoke(tmp_path, doc)
    assert result.exit_code == 2
    assert "outside [0, 1]" in result.output


def test_a_baseline_without_a_threshold_is_refused(tmp_path: Path) -> None:
    doc = {"run": {"run_id": "r1", "calls": _CALLS}, "baseline": {"calls": _CALLS}}
    result = _invoke(tmp_path, doc)
    assert result.exit_code == 2


def test_a_missing_document_exits_two(tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["drift", "fingerprint", str(tmp_path / "nope.json")]
    )
    assert result.exit_code == 2


def test_a_malformed_document_exits_two(tmp_path: Path) -> None:
    p = tmp_path / "bad.json"
    p.write_text("{not json")
    assert runner.invoke(app, ["drift", "fingerprint", str(p)]).exit_code == 2


def test_help_smoke() -> None:
    result = runner.invoke(app, ["drift", "fingerprint", "--help"])
    assert result.exit_code == 0
    assert "fingerprint" in result.output.lower()
