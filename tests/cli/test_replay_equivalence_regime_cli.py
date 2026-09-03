"""nova replay-equivalence regime — CLI surface (ADR-0144 D3 input)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()


def _capsule(tmp_path: Path, lines: list[dict] | str) -> Path:
    d = tmp_path / "cap"
    d.mkdir(exist_ok=True)
    body = lines if isinstance(lines, str) else "\n".join(json.dumps(x) for x in lines)
    (d / "model-calls.jsonl").write_text(body + "\n")
    return d


def test_a_fully_pinned_run_exits_zero(tmp_path: Path) -> None:
    cap = _capsule(tmp_path, [{"temperature": 0, "seed": 42}])
    result = runner.invoke(app, ["replay-equivalence", "regime", "--capsule", str(cap)])

    assert result.exit_code == 0, result.output
    assert '"eligibility": "eligible"' in result.output.replace("'", '"')


def test_a_hot_run_exits_one(tmp_path: Path) -> None:
    cap = _capsule(tmp_path, [{"temperature": 1.2, "seed": 1}])
    result = runner.invoke(app, ["replay-equivalence", "regime", "--capsule", str(cap)])

    assert result.exit_code == 1, result.output
    assert "not-eligible" in result.output


def test_an_unrecorded_temperature_exits_one_as_unknown(tmp_path: Path) -> None:
    """Unknown is not eligible — absence of evidence is not evidence."""
    cap = _capsule(tmp_path, [{"seed": 1}])
    result = runner.invoke(app, ["replay-equivalence", "regime", "--capsule", str(cap)])

    assert result.exit_code == 1, result.output
    assert "unknown" in result.output


def test_a_run_with_no_calls_is_unknown(tmp_path: Path) -> None:
    cap = _capsule(tmp_path, "")
    result = runner.invoke(app, ["replay-equivalence", "regime", "--capsule", str(cap)])

    assert result.exit_code == 1, result.output
    assert "unknown" in result.output


def test_the_reasons_are_printed(tmp_path: Path) -> None:
    cap = _capsule(tmp_path, [{"temperature": 0.9, "seed": 1}])
    result = runner.invoke(app, ["replay-equivalence", "regime", "--capsule", str(cap)])

    assert "non-zero temperature" in result.output


@pytest.mark.parametrize(
    ("body", "why"),
    [("{not json", "unparseable line"), ("[1,2,3]", "line is not an object")],
)
def test_a_malformed_model_calls_file_exits_two(
    body: str, why: str, tmp_path: Path
) -> None:
    cap = _capsule(tmp_path, body)
    result = runner.invoke(app, ["replay-equivalence", "regime", "--capsule", str(cap)])
    assert result.exit_code == 2, f"{why}: {result.output}"


def test_a_missing_capsule_exits_two(tmp_path: Path) -> None:
    result = runner.invoke(app, ["replay-equivalence", "regime",
                                 "--capsule", str(tmp_path / "nope")])
    assert result.exit_code == 2


def test_a_capsule_without_model_calls_exits_two(tmp_path: Path) -> None:
    d = tmp_path / "cap"
    d.mkdir()
    result = runner.invoke(app, ["replay-equivalence", "regime", "--capsule", str(d)])
    assert result.exit_code == 2
