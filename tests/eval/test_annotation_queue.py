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

"""Annotation-queue record models (ADR-0118 P1) — schema + fixture conformance.

Golden fixtures live in ``tests/fixtures/annotation-queue/`` (4 valid + 9
invalid, graduated from the accepted design draft). Each fixture must behave as
its name says under BOTH the promoted JSON Schemas and the Pydantic models.
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest
from pydantic import ValidationError

from novafabric.eval.annotation_queue import (
    ANNOTATION_QUEUE_SCHEMA_VERSION,
    AnnotationQueue,
    AssignmentPolicy,
    ItemState,
    QueueItem,
    SubjectSelector,
    confirmation_payload,
    submission_payload,
)

_ROOT = Path(__file__).resolve().parents[2]
_FIXTURES = _ROOT / "tests" / "fixtures" / "annotation-queue"
_QUEUE_SCHEMA = json.loads((_ROOT / "schemas" / "annotation-queue.schema.json").read_text())
_ITEM_SCHEMA = json.loads(
    (_ROOT / "schemas" / "annotation-queue-item.schema.json").read_text()
)

_QUEUE_VALIDATOR = jsonschema.Draft202012Validator(
    _QUEUE_SCHEMA, format_checker=jsonschema.FormatChecker()
)
_ITEM_VALIDATOR = jsonschema.Draft202012Validator(
    _ITEM_SCHEMA, format_checker=jsonschema.FormatChecker()
)

_SUBJECT = "sha256:" + "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
_QUEUE_ID = "01HXB0Q7M4YZ2K7N9DPBYK2WX0"

_QUEUE_FIXTURES = sorted(_FIXTURES.glob("queue-*.json"))
_ITEM_FIXTURES = sorted(_FIXTURES.glob("item-*.json"))


def _queue(**over: object) -> AnnotationQueue:
    base: dict[str, object] = dict(name="q1", criteria=["factuality"])
    base.update(over)
    return AnnotationQueue.model_validate(base)


def _item(**over: object) -> QueueItem:
    base: dict[str, object] = dict(queue_id=_QUEUE_ID, subject=_SUBJECT)
    base.update(over)
    return QueueItem.model_validate(base)


# ── model construction ────────────────────────────────────────────────────────


def test_queue_defaults() -> None:
    q = _queue()
    assert q.schema_version == ANNOTATION_QUEUE_SCHEMA_VERSION
    assert q.assignment_policy is AssignmentPolicy.ROUND_ROBIN
    assert q.require_checker is False and q.seal is False
    assert q.subject_selector == SubjectSelector()
    assert len(q.queue_id) == 26


def test_item_defaults_pending() -> None:
    item = _item()
    assert item.state is ItemState.PENDING
    assert item.assignee is None and item.checker is None
    assert item.resulting_score_ids == []


@pytest.mark.parametrize(
    "over",
    [
        {"criteria": []},
        {"criteria": ["a", "a"]},
        {"criteria": [""]},
        {"queue_id": "not-a-ulid"},
        {"schema_version": "9.9.9"},
        {"unknown_key": True},
    ],
)
def test_queue_rejects(over: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _queue(**over)


@pytest.mark.parametrize(
    "over",
    [
        {"subject": "sha256:short"},
        {"subject": "md5:" + "0" * 64},
        {"subject_kind": "row"},
        {"item_id": "nope"},
        {"queue_id": "nope"},
        {"state": "in_review"},
        {"resulting_score_ids": ["nope"]},
        {"schema_version": "0.0.1"},
        {"unknown_key": 1},
    ],
)
def test_item_rejects(over: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _item(**over)


def test_item_checker_must_differ_from_assignee() -> None:
    with pytest.raises(ValidationError, match="separation of duties"):
        _item(
            state="checker_pending",
            assignee="reviewer:a",
            checker="reviewer:a",
        )
    # Distinct identities are fine.
    item = _item(
        state="completed",
        assignee="reviewer:a",
        checker="reviewer:b",
        completed_at="2026-07-15T00:00:00+00:00",
    )
    assert item.checker == "reviewer:b"


def test_completed_item_requires_completed_at() -> None:
    with pytest.raises(ValidationError, match="completed_at"):
        _item(state="completed", assignee="reviewer:a")


def test_selector_sample_bounds_and_run_ids() -> None:
    with pytest.raises(ValidationError):
        SubjectSelector(sample=0.0)
    with pytest.raises(ValidationError):
        SubjectSelector(sample=1.5)
    with pytest.raises(ValidationError, match="ULID"):
        SubjectSelector(run_ids=["nope"])
    # Additive keys are allowed by design (spec §Subject selector).
    sel = SubjectSelector.model_validate({"sample": 1.0, "io.example.custom": "x"})
    assert sel.sample == 1.0


# ── golden fixtures: JSON Schema + Pydantic agreement ─────────────────────────


def test_fixture_inventory() -> None:
    assert len(_QUEUE_FIXTURES) == 6  # 2 valid + 4 invalid
    assert len(_ITEM_FIXTURES) == 7  # 3 valid + 4 invalid


@pytest.mark.parametrize("path", _QUEUE_FIXTURES, ids=lambda p: p.name)
def test_queue_fixtures_behave_as_named(path: Path) -> None:
    payload = json.loads(path.read_text())
    if "invalid" in path.name:
        assert not _QUEUE_VALIDATOR.is_valid(payload), path.name
        with pytest.raises(ValidationError):
            AnnotationQueue.model_validate(payload)
    else:
        _QUEUE_VALIDATOR.validate(payload)
        queue = AnnotationQueue.model_validate(payload)
        # Round-trip stays schema-conformant.
        _QUEUE_VALIDATOR.validate(json.loads(queue.model_dump_json(exclude_none=True)))


@pytest.mark.parametrize("path", _ITEM_FIXTURES, ids=lambda p: p.name)
def test_item_fixtures_behave_as_named(path: Path) -> None:
    payload = json.loads(path.read_text())
    if "invalid" in path.name:
        assert not _ITEM_VALIDATOR.is_valid(payload), path.name
        with pytest.raises(ValidationError):
            QueueItem.model_validate(payload)
    else:
        _ITEM_VALIDATOR.validate(payload)
        item = QueueItem.model_validate(payload)
        _ITEM_VALIDATOR.validate(json.loads(item.model_dump_json(exclude_none=True)))


def test_fresh_records_conform_to_promoted_schemas() -> None:
    q = _queue(require_checker=True, seal=True, description="d")
    _QUEUE_VALIDATOR.validate(json.loads(q.model_dump_json(exclude_none=True)))
    item = _item(capsule_ref="capsules/run-x/")
    _ITEM_VALIDATOR.validate(json.loads(item.model_dump_json(exclude_none=True)))


# ── signature pre-images ──────────────────────────────────────────────────────


def test_signature_payloads_are_deterministic() -> None:
    p1 = submission_payload("i", _SUBJECT, ["a", "b"], "t")
    assert p1 == submission_payload("i", _SUBJECT, ["a", "b"], "t")
    assert p1 != submission_payload("i", _SUBJECT, ["b", "a"], "t")
    c1 = confirmation_payload("i", "checker", "t")
    assert c1 == b"annotation-confirm|i|checker|t"
