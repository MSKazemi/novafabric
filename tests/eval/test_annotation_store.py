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

"""Annotation-queue workflow (ADR-0118 P2–P4): claim, submit, maker-checker, skip.

The keyring is redirected into ``tmp_path`` so Ed25519 material never touches
the developer's real ``~/.config/novafabric/keyring``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import novafabric.trust.keyring as kr
from novafabric.eval.annotation_queue import (
    AssignmentPolicy,
    CriteriaError,
    ItemNotFoundError,
    ItemState,
    ItemStateError,
    QueueExistsError,
    QueueNotFoundError,
    SeparationOfDutiesError,
    SubjectMismatchError,
    SubjectSelector,
    confirmation_payload,
    submission_payload,
)
from novafabric.eval.annotation_store import (
    annotation_subject_digest,
    claim_item,
    claim_next,
    confirm_item,
    create_queue,
    enqueue_item,
    get_item,
    get_queue,
    list_items,
    list_queues,
    queue_progress,
    skip_item,
    submit_item,
)
from novafabric.eval.score_config import ScoreCategory, ScoreConfigViolation, ScoreRange
from novafabric.eval.score_config_catalog import register_config
from novafabric.eval.scores import SCORES_FILENAME, ScoreSource, ScoreValueType, read_scores

_SUBJECT = "sha256:" + "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"


@pytest.fixture(autouse=True)
def iso_keyring(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(kr, "_KEYRING_DIR", tmp_path / "keyring")


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    """A registry DB pre-seeded with the three score configs used by the tests."""
    db = tmp_path / "registry.db"
    register_config(
        "factuality", ScoreValueType.BOOLEAN, "Is it factually correct?", db_path=db
    )
    register_config(
        "helpfulness",
        ScoreValueType.CATEGORICAL,
        "How helpful?",
        categories=[ScoreCategory(value="bad"), ScoreCategory(value="good")],
        db_path=db,
    )
    register_config(
        "toxicity",
        ScoreValueType.NUMERIC,
        "Lower is better.",
        range_=ScoreRange(min=0, max=1),
        db_path=db,
    )
    return db


@pytest.fixture()
def capsule(tmp_path: Path) -> Path:
    cap = tmp_path / "capsule"
    cap.mkdir()
    (cap / "capsule.yaml").write_text("run_id: 01HXAY7M5JZ8R7K4P9DPBYK2WX\n")
    (cap / "trace.jsonl").write_text('{"event": "start"}\n')
    return cap


def _mk_queue(db: Path, **over: object):
    kwargs: dict[str, object] = dict(name="q1", criteria=["factuality", "toxicity"])
    kwargs.update(over)
    return create_queue(db_path=db, **kwargs)  # type: ignore[arg-type]


def _enqueue(db: Path, capsule: Path, queue_ref: str = "q1", subject: str = _SUBJECT):
    return enqueue_item(
        queue_ref, subject=subject, subject_kind="span",
        capsule_ref=str(capsule), db_path=db,
    )


# ── queues ────────────────────────────────────────────────────────────────────


def test_create_get_list_queue(db: Path) -> None:
    q = _mk_queue(db, description="review q")
    assert get_queue("q1", db_path=db).queue_id == q.queue_id
    assert get_queue(q.queue_id, db_path=db).name == "q1"
    assert [x.name for x in list_queues(db_path=db)] == ["q1"]


def test_duplicate_queue_name_refused(db: Path) -> None:
    _mk_queue(db)
    with pytest.raises(QueueExistsError):
        _mk_queue(db)


def test_unknown_queue_ref(db: Path) -> None:
    with pytest.raises(QueueNotFoundError):
        get_queue("nope", db_path=db)


def test_queue_criteria_must_be_registered_configs(db: Path) -> None:
    with pytest.raises(CriteriaError, match="no registered score config"):
        _mk_queue(db, criteria=["factuality", "unregistered_metric"])


def test_queue_progress_zero_filled(db: Path, capsule: Path) -> None:
    _mk_queue(db)
    _enqueue(db, capsule)
    progress = queue_progress("q1", db_path=db)
    assert progress == {
        "pending": 1, "assigned": 0, "checker_pending": 0, "completed": 0, "skipped": 0,
    }


# ── enqueue + selector guard ──────────────────────────────────────────────────


def test_enqueue_and_list_items(db: Path, capsule: Path) -> None:
    _mk_queue(db)
    item = _enqueue(db, capsule)
    assert item.state is ItemState.PENDING and item.assignee is None
    assert [i.item_id for i in list_items("q1", db_path=db)] == [item.item_id]
    assert get_item(item.item_id, db_path=db).subject == _SUBJECT
    assert list_items("q1", state=ItemState.ASSIGNED, db_path=db) == []


def test_selector_subject_kind_guard(db: Path, capsule: Path) -> None:
    _mk_queue(db, subject_selector=SubjectSelector(subject_kind="capsule"))
    with pytest.raises(SubjectMismatchError, match="subject_kind"):
        _enqueue(db, capsule)  # enqueues a span


def test_get_unknown_item(db: Path) -> None:
    with pytest.raises(ItemNotFoundError):
        get_item("01HXB0Q8N5YZ2K7N9DPBYK2WX1", db_path=db)


# ── claim ─────────────────────────────────────────────────────────────────────


def test_claim_next_round_robin_oldest_first(db: Path, capsule: Path) -> None:
    _mk_queue(db)
    first = _enqueue(db, capsule)
    second = _enqueue(db, capsule)
    claimed = claim_next("reviewer:a", queue_ref="q1", db_path=db)
    assert claimed is not None
    assert claimed.item_id == first.item_id
    assert claimed.state is ItemState.ASSIGNED
    assert claimed.assignee == "reviewer:a" and claimed.assigned_at is not None
    # Next claim gets the second item; then the queue is empty.
    next_claim = claim_next("reviewer:b", queue_ref="q1", db_path=db)
    assert next_claim is not None and next_claim.item_id == second.item_id
    assert claim_next("reviewer:c", queue_ref="q1", db_path=db) is None


def test_claim_next_empty_queue_returns_none(db: Path) -> None:
    _mk_queue(db)
    assert claim_next("reviewer:a", queue_ref="q1", db_path=db) is None


def test_claim_named_item_manual_policy(db: Path, capsule: Path) -> None:
    _mk_queue(db, assignment_policy=AssignmentPolicy.MANUAL)
    item = _enqueue(db, capsule)
    claimed = claim_item(item.item_id, "reviewer:a", db_path=db)
    assert claimed.state is ItemState.ASSIGNED
    with pytest.raises(ItemStateError, match="not 'pending'"):
        claim_item(item.item_id, "reviewer:b", db_path=db)


# ── submit → HUMAN scores ─────────────────────────────────────────────────────


def test_submit_writes_human_scores_with_provenance(db: Path, capsule: Path) -> None:
    _mk_queue(db)
    item = _enqueue(db, capsule)
    claim_next("reviewer:a", db_path=db)
    updated, scores = submit_item(
        item.item_id, {"factuality": "true", "toxicity": "0.2"}, db_path=db
    )
    assert updated.state is ItemState.COMPLETED
    assert updated.completed_at is not None
    assert updated.resulting_score_ids == [s.score_id for s in scores]

    on_disk = read_scores(capsule / SCORES_FILENAME)
    assert [s.name for s in on_disk] == ["factuality", "toxicity"]
    by_name = {s.name: s for s in on_disk}
    fact = by_name["factuality"]
    assert fact.source is ScoreSource.HUMAN
    assert fact.evaluator_id == "reviewer:a"
    assert fact.value is True and fact.value_type is ScoreValueType.BOOLEAN
    assert fact.subject == item.subject and fact.subject_kind == "span"
    tox = by_name["toxicity"]
    assert tox.value == 0.2 and tox.value_type is ScoreValueType.NUMERIC
    # eval_card_digest pins the governing score config (content-addressed).
    from novafabric.eval.score_config_catalog import get_config

    assert fact.eval_card_digest == get_config("factuality", db_path=db).content_digest
    assert tox.eval_card_digest == get_config("toxicity", db_path=db).content_digest


def test_submit_is_ed25519_signed_by_the_maker(db: Path, capsule: Path) -> None:
    _mk_queue(db)
    item = _enqueue(db, capsule)
    claim_next("reviewer:a", db_path=db)
    updated, _scores = submit_item(
        item.item_id, {"factuality": "true", "toxicity": "0"}, db_path=db
    )
    ext = updated.extensions or {}
    fp = ext["io.novafabric.annotation.maker_key_fp"]
    sig = ext["io.novafabric.annotation.maker_signature"]
    submitted_at = ext["io.novafabric.annotation.submitted_at"]
    private_key, expected_fp = kr.ensure_keypair("reviewer:a")
    assert fp == expected_fp
    payload = submission_payload(
        updated.item_id, updated.subject, updated.resulting_score_ids, submitted_at
    )
    assert kr.verify_sig(private_key.public_key(), sig, payload)
    # Persisted, not just returned.
    assert (get_item(item.item_id, db_path=db).extensions or {})[
        "io.novafabric.annotation.maker_signature"
    ] == sig


def test_submit_value_violating_config_appends_nothing(db: Path, capsule: Path) -> None:
    _mk_queue(db)
    item = _enqueue(db, capsule)
    claim_next("reviewer:a", db_path=db)
    with pytest.raises(ScoreConfigViolation, match="outside the range"):
        submit_item(item.item_id, {"factuality": "true", "toxicity": "3"}, db_path=db)
    assert not (capsule / SCORES_FILENAME).exists()  # no partial write
    assert get_item(item.item_id, db_path=db).state is ItemState.ASSIGNED  # retryable


@pytest.mark.parametrize(
    ("values", "match"),
    [
        ({"factuality": "true"}, "missing criteria"),
        ({"factuality": "true", "toxicity": "0", "extra": "1"}, "not defined by the queue"),
        ({"factuality": "maybe", "toxicity": "0"}, "expects a boolean"),
        ({"factuality": "true", "toxicity": "hot"}, "expects a number"),
    ],
)
def test_submit_criteria_rejections(
    db: Path, capsule: Path, values: dict[str, str], match: str
) -> None:
    _mk_queue(db)
    item = _enqueue(db, capsule)
    claim_next("reviewer:a", db_path=db)
    with pytest.raises(CriteriaError, match=match):
        submit_item(item.item_id, values, db_path=db)
    assert not (capsule / SCORES_FILENAME).exists()


def test_submit_with_explicit_skip_criterion(db: Path, capsule: Path) -> None:
    _mk_queue(db)
    item = _enqueue(db, capsule)
    claim_next("reviewer:a", db_path=db)
    with pytest.raises(CriteriaError, match="cannot skip"):
        submit_item(item.item_id, {}, skip_criteria=["nope"], db_path=db)
    _updated, scores = submit_item(
        item.item_id, {"factuality": "true"}, skip_criteria=["toxicity"], db_path=db
    )
    assert [s.name for s in scores] == ["factuality"]


def test_submit_all_criteria_skipped_is_refused(db: Path, capsule: Path) -> None:
    _mk_queue(db)
    item = _enqueue(db, capsule)
    claim_next("reviewer:a", db_path=db)
    with pytest.raises(CriteriaError, match="nothing to grade"):
        submit_item(item.item_id, {}, skip_criteria=["factuality", "toxicity"], db_path=db)


def test_submit_categorical_membership(db: Path, capsule: Path) -> None:
    _mk_queue(db, name="q2", criteria=["helpfulness"])
    item = _enqueue(db, capsule, queue_ref="q2")
    claim_next("reviewer:a", db_path=db)
    with pytest.raises(ScoreConfigViolation, match="allowed category"):
        submit_item(item.item_id, {"helpfulness": "amazing"}, db_path=db)
    _updated, scores = submit_item(item.item_id, {"helpfulness": "good"}, db_path=db)
    assert scores[0].value == "good"


def test_submit_requires_assigned_state(db: Path, capsule: Path) -> None:
    _mk_queue(db)
    item = _enqueue(db, capsule)
    with pytest.raises(ItemStateError, match="not 'assigned'"):
        submit_item(item.item_id, {"factuality": "true", "toxicity": "0"}, db_path=db)


def test_submit_by_non_assignee_is_refused(db: Path, capsule: Path) -> None:
    _mk_queue(db)
    item = _enqueue(db, capsule)
    claim_next("reviewer:a", db_path=db)
    with pytest.raises(SeparationOfDutiesError, match="only the assignee"):
        submit_item(
            item.item_id,
            {"factuality": "true", "toxicity": "0"},
            reviewer="reviewer:b",
            db_path=db,
        )


def test_submit_missing_capsule_fails_loudly_and_is_retryable(
    db: Path, capsule: Path
) -> None:
    _mk_queue(db)
    item = _enqueue(db, capsule)
    claim_next("reviewer:a", db_path=db)
    import shutil

    shutil.rmtree(capsule)
    with pytest.raises(ItemStateError, match="capsule directory not found"):
        submit_item(item.item_id, {"factuality": "true", "toxicity": "0"}, db_path=db)
    assert get_item(item.item_id, db_path=db).state is ItemState.ASSIGNED
    # Recreate the capsule: the retry succeeds.
    capsule.mkdir()
    (capsule / "capsule.yaml").write_text("run_id: x\n")
    updated, _ = submit_item(
        item.item_id, {"factuality": "true", "toxicity": "0"}, db_path=db
    )
    assert updated.state is ItemState.COMPLETED


# ── maker-checker (D4) ────────────────────────────────────────────────────────


def test_checker_flow_maker_to_checker_to_completed(db: Path, capsule: Path) -> None:
    _mk_queue(db, require_checker=True)
    item = _enqueue(db, capsule)
    claim_next("reviewer:maker", db_path=db)
    submitted, scores = submit_item(
        item.item_id, {"factuality": "false", "toxicity": "0.9"}, db_path=db
    )
    assert submitted.state is ItemState.CHECKER_PENDING
    assert submitted.completed_at is None
    # The maker's scores are already ordinary immutable evidence on disk.
    assert len(read_scores(capsule / SCORES_FILENAME)) == 2

    confirmed = confirm_item(item.item_id, "reviewer:checker", db_path=db)
    assert confirmed.state is ItemState.COMPLETED
    assert confirmed.checker == "reviewer:checker"
    assert confirmed.completed_at is not None
    assert confirmed.resulting_score_ids == [s.score_id for s in scores]
    # The checker's confirmation is Ed25519-signed and verifiable.
    ext = confirmed.extensions or {}
    private_key, fp = kr.ensure_keypair("reviewer:checker")
    assert ext["io.novafabric.annotation.checker_key_fp"] == fp
    payload = confirmation_payload(
        confirmed.item_id, "reviewer:checker", ext["io.novafabric.annotation.confirmed_at"]
    )
    assert kr.verify_sig(
        private_key.public_key(),
        ext["io.novafabric.annotation.checker_signature"],
        payload,
    )


def test_checker_must_differ_from_maker_identity(db: Path, capsule: Path) -> None:
    _mk_queue(db, require_checker=True)
    item = _enqueue(db, capsule)
    claim_next("reviewer:maker", db_path=db)
    submit_item(item.item_id, {"factuality": "true", "toxicity": "0"}, db_path=db)
    with pytest.raises(SeparationOfDutiesError, match="checker equals the maker"):
        confirm_item(item.item_id, "reviewer:maker", db_path=db)
    assert get_item(item.item_id, db_path=db).state is ItemState.CHECKER_PENDING


def test_checker_key_fingerprint_must_differ(db: Path, capsule: Path) -> None:
    """Two identities sharing one keypair are refused at the crypto level."""
    _mk_queue(db, require_checker=True)
    item = _enqueue(db, capsule)
    claim_next("reviewer:maker", db_path=db)
    submit_item(item.item_id, {"factuality": "true", "toxicity": "0"}, db_path=db)
    # Copy the maker's key under the checker identity (same fingerprint).
    maker_pem = kr._key_path("reviewer:maker")
    checker_pem = kr._key_path("reviewer:sock-puppet")
    checker_pem.write_bytes(maker_pem.read_bytes())
    with pytest.raises(SeparationOfDutiesError, match="fingerprint matches"):
        confirm_item(item.item_id, "reviewer:sock-puppet", db_path=db)


def test_confirm_requires_checker_pending_state(db: Path, capsule: Path) -> None:
    _mk_queue(db)
    item = _enqueue(db, capsule)
    with pytest.raises(ItemStateError, match="not 'checker_pending'"):
        confirm_item(item.item_id, "reviewer:b", db_path=db)


# ── skip ──────────────────────────────────────────────────────────────────────


def test_skip_is_terminal_and_writes_no_score(db: Path, capsule: Path) -> None:
    _mk_queue(db)
    pending = _enqueue(db, capsule)
    skipped = skip_item(pending.item_id, note="out of scope", db_path=db)
    assert skipped.state is ItemState.SKIPPED and skipped.note == "out of scope"
    assert not (capsule / SCORES_FILENAME).exists()
    with pytest.raises(ItemStateError, match="can be skipped"):
        skip_item(pending.item_id, db_path=db)
    # An assigned item can be skipped too.
    assigned = _enqueue(db, capsule)
    claim_next("reviewer:a", db_path=db)
    assert skip_item(assigned.item_id, db_path=db).state is ItemState.SKIPPED
    # But not submitted/completed ones.
    done = _enqueue(db, capsule)
    claim_next("reviewer:a", db_path=db)
    submit_item(done.item_id, {"factuality": "true", "toxicity": "0"}, db_path=db)
    with pytest.raises(ItemStateError):
        skip_item(done.item_id, db_path=db)


# ── subject digest ────────────────────────────────────────────────────────────


def test_annotation_subject_digest_stable_across_annotation(capsule: Path) -> None:
    before = annotation_subject_digest(capsule)
    assert before.startswith("sha256:")
    # Appending annotation streams must not change the subject identity...
    (capsule / SCORES_FILENAME).write_text('{"x": 1}\n')
    (capsule / "comments.jsonl").write_text('{"y": 2}\n')
    assert annotation_subject_digest(capsule) == before
    # ...but changing the evidence itself must.
    (capsule / "trace.jsonl").write_text('{"event": "tampered"}\n')
    assert annotation_subject_digest(capsule) != before
