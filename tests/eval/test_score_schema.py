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

"""Tests for the NF-010 unified ``Score`` schema + ``scores.jsonl`` IO (ADR-0099)."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from novafabric.capture._ulid import new_ulid
from novafabric.eval.scores import (
    SCORES_FILENAME,
    Score,
    ScoreSource,
    ScoreValueType,
    SignificanceBlock,
    append_score,
    boolean_metric_outcomes,
    iter_scores,
    read_scores,
    write_scores,
)

_DIGEST = "sha256:" + "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
_CARD = "sha256:" + "60303ae22b998861bce3b28f33eec1be758a213c86c93c076dbe9f558c11c752"
_SCHEMA = json.loads(
    (Path(__file__).resolve().parents[2] / "schemas" / "score-v1.schema.json").read_text()
)


def _score(**over: object) -> Score:
    base: dict[str, object] = {
        "subject": _DIGEST,
        "name": "answer_faithfulness",
        "value": 0.82,
        "value_type": ScoreValueType.NUMERIC,
        "source": ScoreSource.JUDGE,
        "evaluator_id": "faithfulness-judge",
        "eval_card_digest": _CARD,
    }
    base.update(over)
    return Score(**base)  # type: ignore[arg-type]


# ── value / value_type agreement (requirement 1) ─────────────────────────────


def test_numeric_score_roundtrips() -> None:
    s = _score()
    again = Score.model_validate_json(s.model_dump_json())
    assert again == s
    assert again.value == 0.82


def test_boolean_score() -> None:
    s = _score(value=True, value_type=ScoreValueType.BOOLEAN, source=ScoreSource.CODE)
    assert s.value is True


def test_categorical_score() -> None:
    s = _score(value="pass", value_type=ScoreValueType.CATEGORICAL, source=ScoreSource.HUMAN)
    assert s.value == "pass"


def test_numeric_rejects_bool() -> None:
    with pytest.raises(ValueError, match="numeric"):
        _score(value=True, value_type=ScoreValueType.NUMERIC)


def test_boolean_requires_bool() -> None:
    with pytest.raises(ValueError, match="boolean"):
        _score(value=1.0, value_type=ScoreValueType.BOOLEAN)


def test_categorical_requires_str() -> None:
    with pytest.raises(ValueError, match="categorical"):
        _score(value=0.5, value_type=ScoreValueType.CATEGORICAL)


# ── content-addressed identity validation (requirement 1/2) ──────────────────


def test_bad_subject_digest() -> None:
    with pytest.raises(ValueError, match="subject"):
        _score(subject="not-a-digest")


def test_bad_eval_card_digest() -> None:
    with pytest.raises(ValueError, match="eval_card_digest"):
        _score(eval_card_digest="sha256:tooshort")


def test_bad_run_id() -> None:
    with pytest.raises(ValueError, match="run_id"):
        _score(run_id="not-a-ulid")


def test_good_run_id_accepted() -> None:
    s = _score(run_id=new_ulid())
    assert s.run_id is not None


def test_bad_score_id() -> None:
    with pytest.raises(ValueError, match="score_id"):
        _score(score_id="not-a-ulid")


def test_iter_skips_blank_lines(tmp_path: Path) -> None:
    path = tmp_path / SCORES_FILENAME
    path.write_text(_score().model_dump_json() + "\n\n" + _score().model_dump_json() + "\n")
    assert len(list(iter_scores(path))) == 2


def test_defaults_are_generated() -> None:
    s = _score()
    assert len(s.score_id) == 26
    assert s.created_at.endswith("+00:00") or s.created_at.endswith("Z")
    assert s.subject_kind == "span"


def test_bad_subject_kind() -> None:
    with pytest.raises(ValueError, match="subject_kind"):
        _score(subject_kind="galaxy")


def test_significance_block() -> None:
    s = _score(
        significance=SignificanceBlock(method="wilson", ci_low=0.71, ci_high=0.9, confidence=0.95)
    )
    assert s.significance is not None
    assert s.significance.method == "wilson"


def test_extra_fields_forbidden() -> None:
    with pytest.raises(ValueError):
        _score(surprise="boo")


# ── JSONL IO + additive-first backward-compat (requirement 3) ────────────────


def test_write_read_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / SCORES_FILENAME
    scores = [_score(), _score(value="good", value_type=ScoreValueType.CATEGORICAL)]
    write_scores(path, scores)
    loaded = read_scores(path)
    assert loaded == scores


def test_append_score(tmp_path: Path) -> None:
    path = tmp_path / SCORES_FILENAME
    append_score(path, _score())
    append_score(path, _score())
    assert len(read_scores(path)) == 2


def test_read_missing_returns_empty(tmp_path: Path) -> None:
    # Additive-first (SPK-EVAL-1): a capsule with no scores.jsonl is valid.
    assert read_scores(tmp_path / "absent.jsonl") == []
    assert list(iter_scores(tmp_path / "absent.jsonl")) == []


def test_read_skips_blank_lines(tmp_path: Path) -> None:
    path = tmp_path / SCORES_FILENAME
    path.write_text(_score().model_dump_json() + "\n\n   \n" + _score().model_dump_json() + "\n")
    assert len(read_scores(path)) == 2


def test_read_invalid_line_raises_with_context(tmp_path: Path) -> None:
    path = tmp_path / SCORES_FILENAME
    path.write_text('{"not": "a score"}\n')
    with pytest.raises(ValueError, match=r"scores\.jsonl:1: invalid Score"):
        read_scores(path)


def test_iter_scores_streams(tmp_path: Path) -> None:
    path = tmp_path / SCORES_FILENAME
    write_scores(path, [_score(), _score()])
    assert len(list(iter_scores(path))) == 2


def test_iter_scores_invalid_line(tmp_path: Path) -> None:
    path = tmp_path / SCORES_FILENAME
    path.write_text("{bad json}\n")
    with pytest.raises(ValueError, match="invalid Score"):
        list(iter_scores(path))


# ── JSON Schema conformance (schemas/score-v1.schema.json) ───────────────────


def test_score_validates_against_json_schema() -> None:
    instance = json.loads(_score(run_id=new_ulid()).model_dump_json(exclude_none=True))
    jsonschema.validate(instance, _SCHEMA)


def test_json_schema_rejects_bad_digest() -> None:
    instance = json.loads(_score().model_dump_json(exclude_none=True))
    instance["subject"] = "nope"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance, _SCHEMA)


# ── boolean_metric_outcomes bridge (NF-007 promote-gate source) ──────────────


def test_boolean_metric_outcomes_filters_and_maps() -> None:
    scores = [
        _score(name="task_pass", value=True, value_type=ScoreValueType.BOOLEAN, source=ScoreSource.CODE),
        _score(name="other", value=False, value_type=ScoreValueType.BOOLEAN, source=ScoreSource.CODE),
        _score(name="task_pass", value=False, value_type=ScoreValueType.BOOLEAN, source=ScoreSource.CODE),
    ]
    assert boolean_metric_outcomes(scores, "task_pass") == [1, 0]


def test_boolean_metric_outcomes_rejects_non_boolean() -> None:
    scores = [_score(name="task_pass")]  # numeric
    with pytest.raises(ValueError, match="not boolean"):
        boolean_metric_outcomes(scores, "task_pass")


def test_boolean_metric_outcomes_empty_when_no_match() -> None:
    assert boolean_metric_outcomes([_score(name="x", value=True,
                                           value_type=ScoreValueType.BOOLEAN,
                                           source=ScoreSource.CODE)], "task_pass") == []
