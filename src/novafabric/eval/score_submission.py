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

"""External score-submission core (ADR-0119) — one validation seam, three surfaces.

This module is the **shared validation core** behind the ADR-0119 submission
surfaces: the public SDK function (:mod:`novafabric.scores`), the ``nova score
submit`` CLI, and the optional server REST endpoints. It ingests an
*externally-computed* evaluation score into a target Run Capsule's append-only
``scores.jsonl`` via the existing :func:`novafabric.eval.scores.append_score` —
it never runs an evaluator and performs no model call.

Validation invariants (spec ``design/spec/score-submission-api-v0.md``,
fail-closed — on any rejection **nothing is written**):

1. **Well-formed** — all shipped :class:`~novafabric.eval.scores.Score`
   invariants hold (:class:`SubmissionInvalidError` otherwise).
2. **Config-valid** — if an ADR-0117 :class:`~novafabric.eval.score_config.ScoreConfig`
   governs the score's ``name``, the value must satisfy it
   (:class:`~novafabric.eval.score_config.ScoreConfigViolation` otherwise); no
   matching config ⇒ accepted with ``config_bound=False``.
3. **Attributed** — a non-empty ``evaluator_id`` (enforced by the ``Score`` model);
   server surfaces additionally bind the authenticated principal.
4. **Subject exists** — the ``subject`` digest must be anchored in the target
   capsule (:class:`SubjectNotFoundError` otherwise; see :func:`capsule_known_digests`).
5. **Append-only** — a correction is a *new* record whose ``supersedes`` names a
   ``score_id`` already present in the same log
   (:class:`SupersedesNotFoundError` otherwise); no line is ever mutated.
6. **Idempotent** — re-submitting an existing ``score_id`` with an identical body
   (``created_at`` excluded — it defaults to submission time) is a no-op returning
   the stored record; a differing body is an idempotency-key collision
   (:class:`IdempotencyConflictError`).
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from novafabric.eval.score_config import ScoreConfig, validate_score_against_config
from novafabric.eval.score_config_catalog import find_config_for_score
from novafabric.eval.scores import (
    SCORES_FILENAME,
    Score,
    ScoreSource,
    ScoreValueType,
    SignificanceBlock,
    append_score,
    read_scores,
)

__all__ = [
    "CapsuleNotFoundError",
    "IdempotencyConflictError",
    "ScoreSubmissionError",
    "ScoreSubmissionRequest",
    "SubjectNotFoundError",
    "SubmissionInvalidError",
    "SubmitResult",
    "SupersedesNotFoundError",
    "capsule_known_digests",
    "submit",
    "submit_request",
]

_SHA256_HEX_RE = re.compile(r"sha256:[0-9a-f]{64}")

#: Top-level capsule evidence files scanned for recorded ``sha256:`` digests
#: (bounded: one read per file, no recursion into inputs/ or outputs/).
_EVIDENCE_GLOBS = ("*.jsonl", "*.json", "*.yaml", "*.lock")


# ── Errors (named, mapped to transport codes by the callers) ──────────────────


class ScoreSubmissionError(Exception):
    """Base class for score-submission rejections. Nothing was written."""


class CapsuleNotFoundError(ScoreSubmissionError):
    """The target capsule directory does not exist / has no ``capsule.yaml`` (404)."""


class SubmissionInvalidError(ScoreSubmissionError):
    """The submitted record is malformed — a shipped ``Score`` invariant fails (400)."""


class SubjectNotFoundError(ScoreSubmissionError):
    """The ``subject`` digest is not anchored in the target capsule (404)."""


class SupersedesNotFoundError(ScoreSubmissionError):
    """``supersedes`` references a ``score_id`` absent from the target log (422)."""


class IdempotencyConflictError(ScoreSubmissionError):
    """``score_id`` reuse with a *different* body — idempotency-key collision (409)."""


# ── Request / result models ───────────────────────────────────────────────────


class ScoreSubmissionRequest(BaseModel):
    """The submission envelope (POST body / SDK argument shape).

    Wire contract: ``schemas/score-submission-request.schema.json``. Unknown keys
    are rejected (closed schema).
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    value: bool | float | str
    value_type: ScoreValueType
    source: ScoreSource
    evaluator_id: str = Field(min_length=1)
    subject: str
    subject_kind: str = "span"
    eval_card_digest: str
    score_id: str | None = None
    supersedes: str | None = None
    run_id: str | None = None
    significance: SignificanceBlock | None = None
    created_at: str | None = None


class SubmitResult(BaseModel):
    """Outcome of an accepted submission (spec ``SubmitResult``).

    ``idempotent_replay`` — the ``score_id`` already existed with an identical
    body; the stored record is returned and no second line was appended.
    ``config_bound`` — a matching ADR-0117 ``ScoreConfig`` governed (and
    validated) the value; ``False`` means the score was accepted unvalidated.
    """

    model_config = ConfigDict(extra="forbid")

    score: Score
    idempotent_replay: bool
    config_bound: bool


# ── Subject anchoring ─────────────────────────────────────────────────────────


def capsule_known_digests(capsule_dir: str | Path) -> set[str]:
    """The ``sha256:`` digests a submission subject may reference in *capsule_dir*.

    A subject "exists in the target capsule" (spec Rule 4) when it is one of:

    - the capsule's **annotation-stable digest** (Merkle root excluding the
      annotation streams — stable across successive score/comment appends);
    - the capsule's current full Merkle root (``capsule_merkle_root``);
    - the digest of ``capsule.yaml`` (the manifest subject stamped by capture);
    - any digest **recorded in the capsule's top-level evidence files**
      (``*.jsonl`` / ``*.json`` / ``*.yaml`` / ``*.lock``) — e.g. a span digest
      stamped by a producer, or the subject of a prior score.

    Bounded, read-only: each top-level evidence file is read once.
    """
    from novafabric.eval.annotation_store import annotation_subject_digest  # noqa: PLC0415
    from novafabric.evidence.merkle import capsule_merkle_root  # noqa: PLC0415

    root = Path(capsule_dir)
    digests: set[str] = {
        annotation_subject_digest(root),
        capsule_merkle_root(root),
        "sha256:" + hashlib.sha256((root / "capsule.yaml").read_bytes()).hexdigest(),
    }
    for pattern in _EVIDENCE_GLOBS:
        for path in sorted(root.glob(pattern)):
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            digests.update(_SHA256_HEX_RE.findall(text))
    return digests


# ── Core submission ───────────────────────────────────────────────────────────


def _body_key(score: Score) -> dict[str, Any]:
    """The idempotency comparison body: every field except ``created_at``.

    ``created_at`` defaults to submission time, so a CI job that re-submits the
    same record (same client-minted ``score_id``) must not be defeated by the
    receipt timestamp.
    """
    return score.model_dump(mode="json", exclude={"created_at"}, exclude_none=True)


def submit_request(
    capsule_dir: str | Path,
    request: ScoreSubmissionRequest,
    *,
    db_path: Path | None = None,
) -> SubmitResult:
    """Validate *request* against *capsule_dir* and append it (the shared core).

    Runs the spec's six validation rules in order; on any rejection **nothing is
    written**. ``db_path`` targets the registry SQLite DB holding ADR-0117 score
    configs (``None`` = the default registry).
    """
    root = Path(capsule_dir)
    if not root.is_dir() or not (root / "capsule.yaml").exists():
        raise CapsuleNotFoundError(f"capsule not found: {root}")

    # Rule 1 — well-formed Score (shipped invariants). ``exclude_none`` lets the
    # Score defaults mint a fresh ULID / receipt timestamp for omitted fields.
    try:
        score = Score(**request.model_dump(exclude_none=True))
    except (ValidationError, ValueError) as exc:
        raise SubmissionInvalidError(f"invalid score: {exc}") from exc

    scores_path = root / SCORES_FILENAME
    existing = read_scores(scores_path)

    # Rule 6 — idempotency by score_id.
    for prior in existing:
        if prior.score_id == score.score_id:
            if _body_key(prior) == _body_key(score):
                governing = find_config_for_score(score.name, db_path=db_path)
                return SubmitResult(
                    score=prior, idempotent_replay=True, config_bound=governing is not None
                )
            raise IdempotencyConflictError(
                f"score_id {score.score_id} already exists with a different body; "
                "mint a new score_id for a genuinely different score"
            )

    # Rule 5 — a correction must point at a record present in the same log.
    if score.supersedes is not None and not any(
        prior.score_id == score.supersedes for prior in existing
    ):
        raise SupersedesNotFoundError(
            f"supersedes references score_id {score.supersedes!r}, "
            f"which is not present in {scores_path}"
        )

    # Rule 4 — the subject must be anchored to captured evidence.
    if score.subject not in capsule_known_digests(root):
        raise SubjectNotFoundError(
            f"subject {score.subject} is not a span/capsule digest of capsule {root.name}"
        )

    # Rule 2 — config-valid (ADR-0117); raises ScoreConfigViolation on violation.
    config: ScoreConfig | None = find_config_for_score(score.name, db_path=db_path)
    if config is not None:
        validate_score_against_config(score, config)

    append_score(scores_path, score)
    return SubmitResult(score=score, idempotent_replay=False, config_bound=config is not None)


def submit(
    capsule_dir: str | Path,
    *,
    name: str,
    value: bool | float | str,
    value_type: ScoreValueType | str,
    evaluator_id: str,
    subject: str,
    source: ScoreSource | str = "code",
    eval_card_digest: str | None = None,
    subject_kind: str = "span",
    supersedes: str | None = None,
    score_id: str | None = None,
    run_id: str | None = None,
    significance: SignificanceBlock | None = None,
    db_path: Path | None = None,
) -> SubmitResult:
    """Submit one externally-computed score into *capsule_dir* (ADR-0119 D1 SDK).

    The documented, stable wrapper over :func:`submit_request` — constructs the
    submission envelope, runs the validation rules, and on success appends exactly
    one ``Score`` line via the existing append-only writer. Works offline with no
    server; performs no model call.
    """
    if eval_card_digest is None:
        raise SubmissionInvalidError(
            "eval_card_digest is required: a score must reference the eval card "
            "(sha256:<hex>) that defines its evaluator"
        )
    try:
        request = ScoreSubmissionRequest(
            name=name,
            value=value,
            value_type=ScoreValueType(value_type),
            source=ScoreSource(source),
            evaluator_id=evaluator_id,
            subject=subject,
            subject_kind=subject_kind,
            eval_card_digest=eval_card_digest,
            score_id=score_id,
            supersedes=supersedes,
            run_id=run_id,
            significance=significance,
        )
    except (ValidationError, ValueError) as exc:
        raise SubmissionInvalidError(f"invalid submission: {exc}") from exc
    return submit_request(capsule_dir, request, db_path=db_path)
