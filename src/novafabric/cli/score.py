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

"""``nova score`` — external score submission (experimental, ADR-0119 P2).

A one-shot, offline append for CI jobs and human tools: validates an
externally-computed score against the target capsule (subject anchoring,
ADR-0117 score config, idempotency, append-only ``supersedes`` corrections)
and appends it to the capsule's ``scores.jsonl``. JSON in / JSON out; on any
rejection **nothing is written** and the exit code is non-zero. No server, no
internet, no model call.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated

import typer

from novafabric.eval.score_config import ScoreConfigViolation
from novafabric.eval.score_submission import (
    CapsuleNotFoundError,
    IdempotencyConflictError,
    ScoreSubmissionError,
    SubjectNotFoundError,
    SubmissionInvalidError,
    SupersedesNotFoundError,
    submit,
)
from novafabric.eval.scores import ScoreSource, ScoreValueType

score_app = typer.Typer(
    help=(
        "Submit externally-computed evaluation scores into a capsule's "
        "append-only scores.jsonl (experimental, ADR-0119)."
    ),
    no_args_is_help=True,
)

#: Machine-readable rejection codes (mirrors the REST error table in the spec).
_ERROR_CODES: tuple[tuple[type[Exception], str], ...] = (
    (SubmissionInvalidError, "invalid_score"),
    (CapsuleNotFoundError, "capsule_not_found"),
    (SubjectNotFoundError, "subject_not_found"),
    (IdempotencyConflictError, "idempotency_conflict"),
    (SupersedesNotFoundError, "supersedes_not_found"),
    (ScoreConfigViolation, "config_violation"),
)


def _coerce_value(raw: str, value_type: ScoreValueType) -> bool | float | str:
    if value_type is ScoreValueType.NUMERIC:
        try:
            return float(raw)
        except ValueError as exc:
            raise typer.BadParameter(f"--value {raw!r} is not numeric") from exc
    if value_type is ScoreValueType.BOOLEAN:
        low = raw.strip().lower()
        if low in ("true", "1", "yes", "pass"):
            return True
        if low in ("false", "0", "no", "fail"):
            return False
        raise typer.BadParameter(f"--value {raw!r} is not a boolean")
    return raw


@score_app.command("submit")
def score_submit(
    capsule: Annotated[Path, typer.Option(help="Target capsule directory.")],
    name: Annotated[str, typer.Option(help="Metric name (matched against a score config).")],
    value: Annotated[str, typer.Option(help="Score value (coerced per --value-type).")],
    evaluator: Annotated[
        str, typer.Option(help="Identity of the evaluator that produced the value.")
    ],
    subject: Annotated[
        str, typer.Option(help="sha256:<hex> of the scored span/capsule (must exist).")
    ],
    eval_card: Annotated[
        str, typer.Option("--eval-card", help="sha256:<hex> digest of the eval card.")
    ],
    value_type: Annotated[
        ScoreValueType, typer.Option(help="boolean|categorical|numeric.")
    ] = ScoreValueType.NUMERIC,
    source: Annotated[
        ScoreSource, typer.Option(help="human|heuristic|code|judge.")
    ] = ScoreSource.CODE,
    subject_kind: Annotated[str, typer.Option(help="span|capsule.")] = "span",
    supersedes: Annotated[
        str | None,
        typer.Option(help="score_id of a prior record this score corrects (append-only)."),
    ] = None,
    score_id: Annotated[
        str | None,
        typer.Option(help="Client-minted ULID idempotency key (omit for a fresh ULID)."),
    ] = None,
    run_id: Annotated[str | None, typer.Option(help="Optional run/capsule ULID.")] = None,
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Emit the full submission envelope, not just the record."),
    ] = False,
) -> None:
    """Submit one externally-computed score into a capsule (append-only, fail-closed).

    On success the appended (or idempotently-replayed) record is echoed to stdout as
    JSON; exit 0. On rejection a structured error is printed to stderr, nothing is
    written, and the exit code is non-zero. Safe to re-run with ``--score-id``.
    """
    try:
        result = submit(
            capsule,
            name=name,
            value=_coerce_value(value, value_type),
            value_type=value_type,
            evaluator_id=evaluator,
            subject=subject,
            source=source,
            eval_card_digest=eval_card,
            subject_kind=subject_kind,
            supersedes=supersedes,
            score_id=score_id,
            run_id=run_id,
        )
    except (ScoreSubmissionError, ScoreConfigViolation) as exc:
        code = next(c for t, c in _ERROR_CODES if isinstance(exc, t))
        print(json.dumps({"error": code, "message": str(exc)}), file=sys.stderr)
        raise typer.Exit(code=1) from exc
    if as_json:
        payload = result.model_dump(mode="json", exclude_none=True)
        payload["score"] = json.loads(result.score.model_dump_json(exclude_none=True))
        print(json.dumps(payload))
    else:
        print(result.score.model_dump_json(exclude_none=True))
