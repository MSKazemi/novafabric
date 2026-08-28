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

"""Public score-submission SDK (ADR-0119, experimental) — ``novafabric.scores``.

The documented, supported surface for submitting an *externally-computed*
evaluation score into a Run Capsule's append-only ``scores.jsonl``::

    from novafabric.scores import submit

    result = submit(
        "./capsules/run-01HX...",
        name="answer_correct", value=True, value_type="boolean",
        source="judge", evaluator_id="ci://acme/repo#judge@v3",
        subject="sha256:...", eval_card_digest="sha256:...",
    )
    print(result.score.score_id, result.config_bound)

Local-first: works with ``pip install novafabric``, offline, no server, no model
call. Validation is fail-closed (on rejection nothing is written), append-only
(a correction is a new record with ``supersedes``), and idempotent by
``score_id``. See ``the private design/spec/score-submission-api-v0.md`` for the contract;
the implementation lives in :mod:`novafabric.eval.score_submission`.
"""

from __future__ import annotations

from novafabric.eval.score_config import ScoreConfigViolation
from novafabric.eval.score_submission import (
    CapsuleNotFoundError,
    IdempotencyConflictError,
    ScoreSubmissionError,
    ScoreSubmissionRequest,
    SubjectNotFoundError,
    SubmissionInvalidError,
    SubmitResult,
    SupersedesNotFoundError,
    submit,
    submit_request,
)
from novafabric.eval.scores import Score, ScoreSource, ScoreValueType, SignificanceBlock

__all__ = [
    "CapsuleNotFoundError",
    "IdempotencyConflictError",
    "Score",
    "ScoreConfigViolation",
    "ScoreSource",
    "ScoreSubmissionError",
    "ScoreSubmissionRequest",
    "ScoreValueType",
    "SignificanceBlock",
    "SubjectNotFoundError",
    "SubmissionInvalidError",
    "SubmitResult",
    "SupersedesNotFoundError",
    "submit",
    "submit_request",
]
