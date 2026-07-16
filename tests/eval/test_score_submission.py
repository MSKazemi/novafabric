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

"""Tests for the ADR-0119 external score-submission core (SDK surface).

Covers the spec's six validation rules end to end against a real temp capsule
and an isolated temp registry DB: fail-closed rejections (nothing written),
append-only corrections via ``supersedes``, idempotent replay by ``score_id``,
subject anchoring, and ADR-0117 config validation at the ingest boundary.
Golden request/response fixtures are schema-verified in the companion test.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from novafabric.capture._ulid import new_ulid
from novafabric.eval.score_config import ScoreConfigViolation, ScoreRange
from novafabric.eval.score_config_catalog import register_config
from novafabric.eval.score_submission import (
    CapsuleNotFoundError,
    IdempotencyConflictError,
    ScoreSubmissionError,
    SubjectNotFoundError,
    SubmissionInvalidError,
    SupersedesNotFoundError,
    capsule_known_digests,
    submit,
)
from novafabric.eval.scores import SCORES_FILENAME, ScoreValueType, read_scores
from novafabric.scores import submit as public_submit

_SPAN = "sha256:" + "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
_CARD = "sha256:" + "60303ae22b998861bce3b28f33eec1be758a213c86c93c076dbe9f558c11c752"
_DANGLING = "sha256:" + "ab" * 32


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "registry.db"


@pytest.fixture()
def capsule(tmp_path: Path) -> Path:
    """Minimal capsule anchoring one span digest in its evidence stream."""
    cap = tmp_path / "capsule"
    cap.mkdir()
    (cap / "capsule.yaml").write_text("run_id: 01HXAY7M5JZ8R7K4P9DPBYK2WX\n")
    (cap / "trace.jsonl").write_text(
        json.dumps({"span_id": "root", "span_digest": _SPAN}) + "\n"
    )
    return cap


def _submit(capsule: Path, db_path: Path, **over: object):
    kwargs: dict[str, object] = dict(
        name="answer_correct",
        value=0.87,
        value_type="numeric",
        evaluator_id="ci://acme/repo#judge@v3",
        subject=_SPAN,
        source="code",
        eval_card_digest=_CARD,
        db_path=db_path,
    )
    kwargs.update(over)
    return submit(capsule, **kwargs)  # type: ignore[arg-type]


# ── happy path ────────────────────────────────────────────────────────────────


def test_submit_appends_with_provenance(capsule: Path, db_path: Path) -> None:
    result = _submit(capsule, db_path)
    assert result.idempotent_replay is False
    assert result.config_bound is False  # no config registered → unvalidated
    scores = read_scores(capsule / SCORES_FILENAME)
    assert len(scores) == 1
    rec = scores[0]
    assert rec.score_id == result.score.score_id
    assert rec.subject == _SPAN
    assert rec.evaluator_id == "ci://acme/repo#judge@v3"
    assert rec.eval_card_digest == _CARD
    assert rec.source.value == "code"
    assert rec.value == 0.87
    assert rec.supersedes is None


def test_public_facade_is_the_same_surface(capsule: Path, db_path: Path) -> None:
    result = public_submit(
        capsule,
        name="m",
        value=True,
        value_type="boolean",
        evaluator_id="annotator:alice",
        subject=_SPAN,
        source="human",
        eval_card_digest=_CARD,
        db_path=db_path,
    )
    assert result.score.name == "m"
    assert read_scores(capsule / SCORES_FILENAME)[0].value is True


def test_capsule_subject_kinds_accepted(capsule: Path, db_path: Path) -> None:
    from novafabric.eval.annotation_store import annotation_subject_digest
    from novafabric.evidence.merkle import capsule_merkle_root

    # The current Merkle root, the annotation-stable digest (unchanged by score
    # appends), and the manifest digest are all anchored capsule subjects.
    for candidate in (
        capsule_merkle_root(capsule),  # first: the appends below change this root
        annotation_subject_digest(capsule),  # stable across score appends
    ):
        result = _submit(
            capsule, db_path, subject=candidate, subject_kind="capsule",
            score_id=new_ulid(),
        )
        assert result.score.subject == candidate
    assert annotation_subject_digest(capsule) in capsule_known_digests(capsule)


# ── rule 1: well-formed ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "over",
    [
        {"value": "yes", "value_type": "boolean"},  # value/value_type disagreement
        {"subject": "not-a-digest"},
        {"eval_card_digest": "sha256:zz"},
        {"score_id": "not-a-ulid"},
        {"supersedes": "not-a-ulid"},
        {"evaluator_id": ""},
        {"value_type": "percentage"},  # unknown enum
        {"source": "oracle"},  # unknown enum
    ],
)
def test_malformed_is_rejected_and_nothing_written(
    capsule: Path, db_path: Path, over: dict[str, object]
) -> None:
    with pytest.raises(SubmissionInvalidError):
        _submit(capsule, db_path, **over)
    assert not (capsule / SCORES_FILENAME).exists()


def test_missing_eval_card_digest_rejected(capsule: Path, db_path: Path) -> None:
    with pytest.raises(SubmissionInvalidError, match="eval_card_digest"):
        _submit(capsule, db_path, eval_card_digest=None)
    assert not (capsule / SCORES_FILENAME).exists()


def test_unknown_capsule_rejected(tmp_path: Path, db_path: Path) -> None:
    with pytest.raises(CapsuleNotFoundError):
        _submit(tmp_path / "missing", db_path)


# ── rule 2: config validation (ADR-0117) ──────────────────────────────────────


def test_config_bound_accept_and_violation_reject(capsule: Path, db_path: Path) -> None:
    register_config(
        name="answer_correct",
        value_type=ScoreValueType.NUMERIC,
        description="0..1 correctness.",
        range_=ScoreRange(min=0.0, max=1.0),
        db_path=db_path,
    )
    ok = _submit(capsule, db_path, value=0.5)
    assert ok.config_bound is True

    with pytest.raises(ScoreConfigViolation):
        _submit(capsule, db_path, value=1.5)
    scores = read_scores(capsule / SCORES_FILENAME)
    assert len(scores) == 1  # the violating submission wrote nothing

    with pytest.raises(ScoreConfigViolation):
        _submit(capsule, db_path, value="high", value_type="categorical")
    assert len(read_scores(capsule / SCORES_FILENAME)) == 1


# ── rule 4: subject anchoring ─────────────────────────────────────────────────


def test_dangling_subject_rejected(capsule: Path, db_path: Path) -> None:
    with pytest.raises(SubjectNotFoundError):
        _submit(capsule, db_path, subject=_DANGLING)
    assert not (capsule / SCORES_FILENAME).exists()


# ── rules 5 + append-only: supersedes corrections ─────────────────────────────


def test_supersedes_appends_and_preserves_history(capsule: Path, db_path: Path) -> None:
    first = _submit(capsule, db_path, value=0.4)
    raw_before = (capsule / SCORES_FILENAME).read_text().splitlines()

    correction = _submit(capsule, db_path, value=0.9, supersedes=first.score.score_id)
    raw_after = (capsule / SCORES_FILENAME).read_text().splitlines()

    # Append-only: the prior line is byte-identical; the correction is a new line.
    assert raw_after[: len(raw_before)] == raw_before
    assert len(raw_after) == len(raw_before) + 1
    scores = read_scores(capsule / SCORES_FILENAME)
    assert [s.value for s in scores] == [0.4, 0.9]
    assert scores[1].supersedes == first.score.score_id
    assert correction.score.score_id != first.score.score_id


def test_supersedes_missing_target_rejected(capsule: Path, db_path: Path) -> None:
    with pytest.raises(SupersedesNotFoundError):
        _submit(capsule, db_path, supersedes=new_ulid())
    assert not (capsule / SCORES_FILENAME).exists()


def test_supersession_chain_allowed(capsule: Path, db_path: Path) -> None:
    a = _submit(capsule, db_path, value=0.1)
    b = _submit(capsule, db_path, value=0.2, supersedes=a.score.score_id)
    c = _submit(capsule, db_path, value=0.3, supersedes=b.score.score_id)
    scores = read_scores(capsule / SCORES_FILENAME)
    assert [s.supersedes for s in scores] == [None, a.score.score_id, b.score.score_id]
    assert c.score.supersedes == b.score.score_id


# ── rule 6: idempotency ───────────────────────────────────────────────────────


def test_idempotent_replay_is_a_noop(capsule: Path, db_path: Path) -> None:
    key = new_ulid()
    first = _submit(capsule, db_path, score_id=key)
    assert first.idempotent_replay is False

    replay = _submit(capsule, db_path, score_id=key)
    assert replay.idempotent_replay is True
    assert replay.score.score_id == key
    assert replay.score.created_at == first.score.created_at  # stored record returned
    assert len(read_scores(capsule / SCORES_FILENAME)) == 1  # no second line


def test_idempotency_key_collision_rejected(capsule: Path, db_path: Path) -> None:
    key = new_ulid()
    _submit(capsule, db_path, score_id=key, value=0.87)
    with pytest.raises(IdempotencyConflictError):
        _submit(capsule, db_path, score_id=key, value=0.11)
    assert len(read_scores(capsule / SCORES_FILENAME)) == 1


def test_fresh_score_ids_append_at_least_once(capsule: Path, db_path: Path) -> None:
    _submit(capsule, db_path)
    _submit(capsule, db_path)  # no client key → a second record, never an overwrite
    assert len(read_scores(capsule / SCORES_FILENAME)) == 2


# ── error taxonomy ────────────────────────────────────────────────────────────


def test_all_rejections_share_the_base_class() -> None:
    for exc_type in (
        CapsuleNotFoundError,
        SubmissionInvalidError,
        SubjectNotFoundError,
        SupersedesNotFoundError,
        IdempotencyConflictError,
    ):
        assert issubclass(exc_type, ScoreSubmissionError)
