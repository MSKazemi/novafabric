# Copyright 2024 NovaFabric Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""CLI tests for ``nova diff --significance`` (NF-007, ADR-0099)."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from novafabric.cli.main import app
from novafabric.eval.scores import Score, ScoreSource, ScoreValueType, write_scores

runner = CliRunner()

_DIGEST = "sha256:" + "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"


def _write(path: Path, successes: int, n: int, metric: str = "task_pass") -> Path:
    fails = n - successes
    acc = 0
    scores: list[Score] = []
    for _ in range(n):
        acc += fails
        passed = not (acc >= n)
        if not passed:
            acc -= n
        scores.append(
            Score(
                subject=_DIGEST,
                name=metric,
                value=passed,
                value_type=ScoreValueType.BOOLEAN,
                source=ScoreSource.CODE,
                evaluator_id="ev",
                eval_card_digest=_DIGEST,
            )
        )
    write_scores(path, scores)
    return path


def test_significant_regression_exits_3(tmp_path: Path) -> None:
    base = _write(tmp_path / "base.jsonl", 48, 50)
    cand = _write(tmp_path / "cand.jsonl", 25, 50)
    result = runner.invoke(
        app,
        ["diff", "--significance", "--baseline", str(base), "--candidate", str(cand),
         "--metric", "task_pass"],
    )
    assert result.exit_code == 3, result.output
    assert "accept_h1" in result.output


def test_no_regression_exits_0(tmp_path: Path) -> None:
    base = _write(tmp_path / "base.jsonl", 48, 50)
    cand = _write(tmp_path / "cand.jsonl", 48, 50)
    result = runner.invoke(
        app,
        ["diff", "--significance", "--baseline", str(base), "--candidate", str(cand)],
    )
    assert result.exit_code == 0, result.output
    assert "accept_h1" not in result.output


def test_json_output(tmp_path: Path) -> None:
    base = _write(tmp_path / "base.jsonl", 48, 50)
    cand = _write(tmp_path / "cand.jsonl", 48, 50)
    result = runner.invoke(
        app,
        ["diff", "--significance", "--baseline", str(base), "--candidate", str(cand), "--json"],
    )
    assert result.exit_code == 0
    assert '"sprt"' in result.output and '"wilson"' in result.output


def test_capsule_dir_baseline(tmp_path: Path) -> None:
    # --baseline may point at a capsule dir (reads <dir>/scores.jsonl).
    cap = tmp_path / "capsule"
    cap.mkdir()
    _write(cap / "scores.jsonl", 25, 50)
    base = _write(tmp_path / "base.jsonl", 48, 50)
    result = runner.invoke(
        app,
        ["diff", "--significance", "--baseline", str(base), "--candidate", str(cap)],
    )
    assert result.exit_code == 3, result.output


def test_missing_metric_is_usage_error(tmp_path: Path) -> None:
    base = _write(tmp_path / "base.jsonl", 48, 50)
    cand = _write(tmp_path / "cand.jsonl", 48, 50)
    result = runner.invoke(
        app,
        ["diff", "--significance", "--baseline", str(base), "--candidate", str(cand),
         "--metric", "nonexistent"],
    )
    assert result.exit_code == 2


def test_non_boolean_metric_rejected(tmp_path: Path) -> None:
    path = tmp_path / "num.jsonl"
    write_scores(
        path,
        [
            Score(
                subject=_DIGEST, name="task_pass", value=0.8, value_type=ScoreValueType.NUMERIC,
                source=ScoreSource.CODE, evaluator_id="ev", eval_card_digest=_DIGEST,
            )
        ],
    )
    result = runner.invoke(
        app, ["diff", "--significance", "--baseline", str(path), "--candidate", str(path)]
    )
    assert result.exit_code == 2


def test_significance_requires_both_sides(tmp_path: Path) -> None:
    base = _write(tmp_path / "base.jsonl", 48, 50)
    result = runner.invoke(app, ["diff", "--significance", "--baseline", str(base)])
    assert result.exit_code == 2


def test_bad_sprt_params_usage_error(tmp_path: Path) -> None:
    base = _write(tmp_path / "base.jsonl", 48, 50)
    cand = _write(tmp_path / "cand.jsonl", 40, 50)
    # p1 > p0 violates the SPRT precondition → usage error, not a crash.
    result = runner.invoke(
        app,
        ["diff", "--significance", "--baseline", str(base), "--candidate", str(cand),
         "--p0", "0.6", "--p1", "0.9"],
    )
    assert result.exit_code == 2


def test_diff_help_smoke() -> None:
    result = runner.invoke(app, ["diff", "--help"])
    assert result.exit_code == 0
    assert "--significance" in result.output


def test_diff_no_args_errors() -> None:
    # Legacy path still requires two refs when not in --significance mode.
    result = runner.invoke(app, ["diff"])
    assert result.exit_code != 0
