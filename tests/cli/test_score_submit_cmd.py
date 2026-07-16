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

"""CLI tests for ``nova score submit`` (ADR-0119 P2).

The ``iso_env`` fixture redirects the registry SQLite DB (``NOVAFABRIC_HOME``)
into a temp dir, so nothing touches real machine state. JSON in / JSON out,
non-zero exit + nothing written on rejection, idempotent re-runs by --score-id.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from novafabric.capture._ulid import new_ulid
from novafabric.cli.main import app
from novafabric.eval.scores import SCORES_FILENAME, read_scores

runner = CliRunner()

_SPAN = "sha256:" + "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
_CARD = "sha256:" + "60303ae22b998861bce3b28f33eec1be758a213c86c93c076dbe9f558c11c752"
_DANGLING = "sha256:" + "ab" * 32


@pytest.fixture(autouse=True)
def iso_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOVAFABRIC_HOME", str(tmp_path / "nfhome"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


@pytest.fixture()
def capsule(tmp_path: Path) -> Path:
    cap = tmp_path / "capsule"
    cap.mkdir()
    (cap / "capsule.yaml").write_text("run_id: 01HXAY7M5JZ8R7K4P9DPBYK2WX\n")
    (cap / "trace.jsonl").write_text(
        json.dumps({"span_id": "root", "span_digest": _SPAN}) + "\n"
    )
    return cap


def _invoke(capsule: Path, *extra: str, subject: str = _SPAN):
    return runner.invoke(
        app,
        [
            "score", "submit",
            "--capsule", str(capsule),
            "--name", "answer_correct",
            "--value", "0.87",
            "--evaluator", "ci://acme/repo#judge@v3",
            "--subject", subject,
            "--eval-card", _CARD,
            *extra,
        ],
    )


def test_submit_happy_path_echoes_record(capsule: Path) -> None:
    result = _invoke(capsule)
    assert result.exit_code == 0, result.output
    record = json.loads(result.stdout.strip())
    assert record["name"] == "answer_correct"
    assert record["value"] == 0.87
    assert record["evaluator_id"] == "ci://acme/repo#judge@v3"
    scores = read_scores(capsule / SCORES_FILENAME)
    assert len(scores) == 1
    assert scores[0].score_id == record["score_id"]


def test_submit_json_envelope(capsule: Path) -> None:
    result = _invoke(capsule, "--json")
    assert result.exit_code == 0, result.output
    envelope = json.loads(result.stdout.strip())
    assert envelope["idempotent_replay"] is False
    assert envelope["config_bound"] is False
    assert envelope["score"]["name"] == "answer_correct"


def test_dangling_subject_rejected_nothing_written(capsule: Path) -> None:
    result = _invoke(capsule, subject=_DANGLING)
    assert result.exit_code == 1
    error = json.loads(result.stderr.strip())
    assert error["error"] == "subject_not_found"
    assert not (capsule / SCORES_FILENAME).exists()


def test_unknown_capsule_rejected(tmp_path: Path) -> None:
    result = _invoke(tmp_path / "missing")
    assert result.exit_code == 1
    assert json.loads(result.stderr.strip())["error"] == "capsule_not_found"


def test_idempotent_rerun_is_safe(capsule: Path) -> None:
    key = new_ulid()
    first = _invoke(capsule, "--score-id", key)
    assert first.exit_code == 0, first.output
    rerun = _invoke(capsule, "--score-id", key, "--json")
    assert rerun.exit_code == 0, rerun.output
    assert json.loads(rerun.stdout.strip())["idempotent_replay"] is True
    assert len(read_scores(capsule / SCORES_FILENAME)) == 1


def test_idempotency_collision_rejected(capsule: Path) -> None:
    key = new_ulid()
    assert _invoke(capsule, "--score-id", key).exit_code == 0
    collision = runner.invoke(
        app,
        [
            "score", "submit", "--capsule", str(capsule),
            "--name", "answer_correct", "--value", "0.11",
            "--evaluator", "ci://acme/repo#judge@v3",
            "--subject", _SPAN, "--eval-card", _CARD,
            "--score-id", key,
        ],
    )
    assert collision.exit_code == 1
    assert json.loads(collision.stderr.strip())["error"] == "idempotency_conflict"
    assert len(read_scores(capsule / SCORES_FILENAME)) == 1


def test_supersedes_correction_appends(capsule: Path) -> None:
    first = _invoke(capsule)
    prior_id = json.loads(first.stdout.strip())["score_id"]
    correction = _invoke(capsule, "--supersedes", prior_id)
    assert correction.exit_code == 0, correction.output
    scores = read_scores(capsule / SCORES_FILENAME)
    assert len(scores) == 2
    assert scores[1].supersedes == prior_id


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("true", True), ("1", True), ("pass", True), ("false", False), ("no", False)],
)
def test_boolean_value_coercion(capsule: Path, raw: str, expected: bool) -> None:
    result = _invoke(
        capsule, "--value-type", "boolean", "--source", "human",
        "--value", raw, "--score-id", new_ulid(),
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout.strip())["value"] is expected


@pytest.mark.parametrize(
    ("value", "value_type"),
    [("not-a-number", "numeric"), ("maybe", "boolean")],
)
def test_uncoercible_value_rejected(capsule: Path, value: str, value_type: str) -> None:
    result = _invoke(capsule, "--value-type", value_type, "--value", value)
    assert result.exit_code != 0
    assert not (capsule / SCORES_FILENAME).exists()


def test_config_violation_rejected(capsule: Path) -> None:
    add = runner.invoke(
        app,
        ["eval", "score", "config", "add", "--name", "answer_correct",
         "--value-type", "numeric", "--description", "0..1 correctness",
         "--min", "0", "--max", "1"],
    )
    assert add.exit_code == 0, add.output
    result = runner.invoke(
        app,
        [
            "score", "submit", "--capsule", str(capsule),
            "--name", "answer_correct", "--value", "1.5",
            "--evaluator", "ci://acme/repo#judge@v3",
            "--subject", _SPAN, "--eval-card", _CARD,
        ],
    )
    assert result.exit_code == 1
    assert json.loads(result.stderr.strip())["error"] == "config_violation"
    assert not (capsule / SCORES_FILENAME).exists()
