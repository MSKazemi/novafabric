from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from typer.testing import CliRunner

from novafabric.cli.main import app
from novafabric.evals.result import EvalResult, Metric

runner = CliRunner()


def _write_result(path: Path, metrics_data: list[dict]) -> Path:  # type: ignore[type-arg]
    result = EvalResult(
        suite_id="test-suite-v1",
        suite_version="0.1.0",
        oci_digest="",
        run_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        capsule_id="cap-001",
        passed=True,
        metrics=[
            Metric(
                name=m["name"],
                value=m["value"],
                unit="score",
                threshold=0.80,
                passed=m["value"] >= 0.80,
            )
            for m in metrics_data
        ],
    )
    path.write_text(result.model_dump_json(indent=2))
    return path


def test_eval_compare_no_regression_exits_0(tmp_path: Path) -> None:
    baseline_file = _write_result(
        tmp_path / "baseline.json",
        [{"name": "accuracy", "value": 0.90}],
    )
    candidate_file = _write_result(
        tmp_path / "candidate.json",
        [{"name": "accuracy", "value": 0.901}],
    )
    result = runner.invoke(
        app,
        ["eval", "compare", str(baseline_file), str(candidate_file)],
    )
    assert result.exit_code == 0, result.output


def test_eval_compare_regression_exits_1(tmp_path: Path) -> None:
    baseline_file = _write_result(
        tmp_path / "baseline.json",
        [{"name": "accuracy", "value": 0.90}],
    )
    candidate_file = _write_result(
        tmp_path / "candidate.json",
        [{"name": "accuracy", "value": 0.50}],
    )
    result = runner.invoke(
        app,
        ["eval", "compare", str(baseline_file), str(candidate_file)],
    )
    assert result.exit_code == 1, result.output


def test_eval_compare_output_contains_metric_name(tmp_path: Path) -> None:
    baseline_file = _write_result(
        tmp_path / "baseline.json",
        [{"name": "accuracy", "value": 0.90}],
    )
    candidate_file = _write_result(
        tmp_path / "candidate.json",
        [{"name": "accuracy", "value": 0.901}],
    )
    result = runner.invoke(
        app,
        ["eval", "compare", str(baseline_file), str(candidate_file)],
    )
    assert "accuracy" in result.output


def test_eval_compare_output_contains_delta(tmp_path: Path) -> None:
    baseline_file = _write_result(
        tmp_path / "baseline.json",
        [{"name": "accuracy", "value": 0.90}],
    )
    candidate_file = _write_result(
        tmp_path / "candidate.json",
        [{"name": "accuracy", "value": 0.80}],
    )
    result = runner.invoke(
        app,
        ["eval", "compare", str(baseline_file), str(candidate_file)],
    )
    # Output should contain a delta value (negative in this case)
    assert "-0.1" in result.output or "−0.1" in result.output or "-0.10" in result.output


def test_eval_compare_custom_alpha(tmp_path: Path) -> None:
    """With a very high alpha (0.50), a small delta should trigger regression."""
    baseline_file = _write_result(
        tmp_path / "baseline.json",
        [{"name": "accuracy", "value": 0.90}],
    )
    candidate_file = _write_result(
        tmp_path / "candidate.json",
        [{"name": "accuracy", "value": 0.86}],  # 4.4% drop
    )
    # alpha=0.50 → 4.4% relative drop > 50%? No. Let's use alpha=0.03 → 4.4% drop > 3%
    result = runner.invoke(
        app,
        ["eval", "compare", str(baseline_file), str(candidate_file), "--alpha", "0.03"],
    )
    assert result.exit_code == 1, result.output


def test_eval_compare_missing_file_exits_1(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "eval", "compare",
            str(tmp_path / "does_not_exist.json"),
            str(tmp_path / "also_missing.json"),
        ],
    )
    assert result.exit_code == 1


def test_eval_compare_help_shows_options() -> None:
    result = runner.invoke(app, ["eval", "compare", "--help"])
    assert result.exit_code == 0
    assert "--alpha" in result.output


def test_eval_compare_invalid_baseline_json_exits_1(tmp_path: Path) -> None:
    bad_file = tmp_path / "bad.json"
    bad_file.write_text("not valid json {{{")
    good_file = _write_result(tmp_path / "good.json", [{"name": "accuracy", "value": 0.90}])
    result = runner.invoke(
        app,
        ["eval", "compare", str(bad_file), str(good_file)],
    )
    assert result.exit_code == 1


def test_eval_compare_invalid_candidate_json_exits_1(tmp_path: Path) -> None:
    good_file = _write_result(tmp_path / "good.json", [{"name": "accuracy", "value": 0.90}])
    bad_file = tmp_path / "bad.json"
    bad_file.write_text("not valid json {{{")
    result = runner.invoke(
        app,
        ["eval", "compare", str(good_file), str(bad_file)],
    )
    assert result.exit_code == 1


def test_eval_compare_no_common_metrics_exits_0(tmp_path: Path) -> None:
    baseline_file = _write_result(
        tmp_path / "baseline.json",
        [{"name": "accuracy", "value": 0.90}],
    )
    candidate_file = _write_result(
        tmp_path / "candidate.json",
        [{"name": "f1_score", "value": 0.88}],
    )
    result = runner.invoke(
        app,
        ["eval", "compare", str(baseline_file), str(candidate_file)],
    )
    assert result.exit_code == 0
    assert "No metrics in common" in result.output
