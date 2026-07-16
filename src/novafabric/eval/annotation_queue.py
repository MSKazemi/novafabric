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

"""Human annotation queue records (ADR-0118, annotation-queue v0).

A **queue** routes review subjects (spans or capsules) to human reviewers; an
**item** tracks one subject through ``pending → assigned → completed`` (with an
optional maker-checker ``checker_pending`` detour, ADR-0118 D4). Completion
writes ordinary ``HUMAN``-source :class:`~novafabric.eval.scores.Score` records
into the subject capsule's append-only ``scores.jsonl`` — the queue is a
*workflow* layer only; it introduces no new evidence format (ADR-0118 D3).

Wire contracts: ``schemas/annotation-queue.schema.json`` and
``schemas/annotation-queue-item.schema.json`` (both closed except the
``subject_selector`` / ``extensions`` objects). The metrics a reviewer grades
are **score configs** (ADR-0117), referenced by name — never inlined.

Invariants enforced here (spec ``design/spec/annotation-queue-v0.md``):

* ULID identities; ``sha256:`` content-addressed ``subject`` (identical form to
  ``Score.subject``).
* ``checker`` MUST differ from ``assignee`` (separation of duties, ADR-0003
  pattern) — a record violating this never parses.
* ``completed`` items carry ``completed_at``.

The SQLite store and the workflow transitions live in
:mod:`novafabric.eval.annotation_store`. This module is pure data: no IO.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from novafabric.capture._ulid import new_ulid

ANNOTATION_QUEUE_SCHEMA_VERSION = "0.1.0"

_ULID_RE = re.compile(r"^[0-7][0-9A-HJKMNP-TV-Z]{25}$")
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Errors ─────────────────────────────────────────────────────────────────────


class AnnotationError(Exception):
    """Base class for annotation-queue errors (ADR-0118)."""


class QueueExistsError(AnnotationError):
    """A queue with this name already exists (names are unique per store)."""


class QueueNotFoundError(AnnotationError):
    """No queue matches the given id or name."""


class ItemNotFoundError(AnnotationError):
    """No queue item matches the given id."""


class ItemStateError(AnnotationError):
    """The requested transition is not legal from the item's current state."""


class CriteriaError(AnnotationError):
    """The submitted scores do not cover the queue's criteria, or a criterion
    has no registered score config (ADR-0117), or a value cannot be coerced."""


class SeparationOfDutiesError(AnnotationError):
    """Maker-checker violation: the checker equals the maker by identity or by
    Ed25519 key fingerprint (ADR-0118 D4 / ADR-0003)."""


class SubjectMismatchError(AnnotationError):
    """The subject being enqueued does not match the queue's ``subject_selector``."""


# ── Enums ──────────────────────────────────────────────────────────────────────


class AssignmentPolicy(str, Enum):
    """How ``nova annotate next`` picks an item (ADR-0118 D1)."""

    ROUND_ROBIN = "round-robin"
    MANUAL = "manual"


class ItemState(str, Enum):
    """Lifecycle of a queue item (ADR-0118 D2)."""

    PENDING = "pending"
    ASSIGNED = "assigned"
    CHECKER_PENDING = "checker_pending"
    COMPLETED = "completed"
    SKIPPED = "skipped"


#: States from which no further transition is possible.
TERMINAL_ITEM_STATES = frozenset({ItemState.COMPLETED, ItemState.SKIPPED})


# ── Records ────────────────────────────────────────────────────────────────────


class SubjectSelector(BaseModel):
    """Declarative filter naming which subjects enter a queue (spec §Subject
    selector). All present keys are ANDed; extra keys are allowed additively.

    The first shipped slice enforces the selector as a **guard at enqueue time**
    (``subject_kind`` agreement); automatic population by enumerating stored
    capsules (ADR-0118 P2 selector evaluation) is planned.
    """

    model_config = ConfigDict(extra="allow")

    subject_kind: Literal["span", "capsule"] | None = None
    run_ids: list[str] | None = None
    tool_names: list[str] | None = None
    tags: list[str] | None = None
    sample: float | None = Field(default=None, gt=0.0, le=1.0)

    @model_validator(mode="after")
    def _check(self) -> SubjectSelector:
        for run_id in self.run_ids or []:
            if not _ULID_RE.match(run_id):
                raise ValueError(f"run_ids entry is not a valid ULID: {run_id!r}")
        return self


class AnnotationQueue(BaseModel):
    """A named routing unit: *what needs reviewing and how it is graded* (D1).

    ``criteria`` are score-config **names** (ADR-0117) — the config catalog is
    the source of truth for each metric's ``value_type`` and allowed values, so
    the graded criterion stays content-addressed and reproducible.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: str = ANNOTATION_QUEUE_SCHEMA_VERSION
    queue_id: str = Field(default_factory=new_ulid)
    name: str = Field(min_length=1)
    criteria: list[str] = Field(min_length=1)
    assignment_policy: AssignmentPolicy = AssignmentPolicy.ROUND_ROBIN
    subject_selector: SubjectSelector = Field(default_factory=SubjectSelector)
    created_at: str = Field(default_factory=_now_iso)
    require_checker: bool = False
    seal: bool = False
    description: str | None = None
    extensions: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _check(self) -> AnnotationQueue:
        if self.schema_version != ANNOTATION_QUEUE_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported schema_version {self.schema_version!r} "
                f"(expected {ANNOTATION_QUEUE_SCHEMA_VERSION!r})"
            )
        if not _ULID_RE.match(self.queue_id):
            raise ValueError(f"queue_id is not a valid ULID: {self.queue_id!r}")
        for criterion in self.criteria:
            if not criterion:
                raise ValueError("criteria entries must be non-empty score-config names")
        if len(set(self.criteria)) != len(self.criteria):
            raise ValueError("criteria must not contain duplicate score-config names")
        return self


class QueueItem(BaseModel):
    """One subject moving through its review lifecycle (D2).

    ``subject`` reuses the ``Score.subject`` content-addressing exactly, so an
    item and the scores it produces are joined by digest. Maker-checker state is
    carried by ``state``/``checker`` on the **item** — never on a ``Score`` (the
    ``Score`` model is closed and scores are append-only evidence, D4).
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: str = ANNOTATION_QUEUE_SCHEMA_VERSION
    item_id: str = Field(default_factory=new_ulid)
    queue_id: str
    subject: str
    subject_kind: str = "span"
    state: ItemState = ItemState.PENDING
    assignee: str | None = None
    checker: str | None = None
    assigned_at: str | None = None
    completed_at: str | None = None
    resulting_score_ids: list[str] = Field(default_factory=list)
    capsule_ref: str | None = None
    note: str | None = None
    extensions: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _check(self) -> QueueItem:
        if self.schema_version != ANNOTATION_QUEUE_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported schema_version {self.schema_version!r} "
                f"(expected {ANNOTATION_QUEUE_SCHEMA_VERSION!r})"
            )
        for label, value in (("item_id", self.item_id), ("queue_id", self.queue_id)):
            if not _ULID_RE.match(value):
                raise ValueError(f"{label} is not a valid ULID: {value!r}")
        if not _SHA256_RE.match(self.subject):
            raise ValueError(f"subject must be a 'sha256:<hex>' digest: {self.subject!r}")
        if self.subject_kind not in ("span", "capsule"):
            raise ValueError(
                f"subject_kind must be 'span' or 'capsule', got {self.subject_kind!r}"
            )
        for score_id in self.resulting_score_ids:
            if not _ULID_RE.match(score_id):
                raise ValueError(f"resulting_score_ids entry is not a ULID: {score_id!r}")
        if self.checker is not None and self.checker == self.assignee:
            raise ValueError(
                "checker must differ from assignee (separation of duties, ADR-0118 D4)"
            )
        if self.state is ItemState.COMPLETED and self.completed_at is None:
            raise ValueError("a completed item must carry completed_at")
        return self


# ── Signature pre-images (deterministic, so signatures stay verifiable) ───────


def submission_payload(
    item_id: str, subject: str, score_ids: list[str], submitted_at: str
) -> bytes:
    """Canonical bytes the maker signs when submitting an item (Ed25519, ADR-0058
    keyring): binds the item, the subject digest, and the written score ULIDs."""
    return (
        f"annotation-submit|{item_id}|{subject}|{','.join(score_ids)}|{submitted_at}"
    ).encode()


def confirmation_payload(item_id: str, checker: str, confirmed_at: str) -> bytes:
    """Canonical bytes the checker signs when confirming a ``checker_pending`` item."""
    return f"annotation-confirm|{item_id}|{checker}|{confirmed_at}".encode()
