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

"""A/B experiment comparison feeding the existing significance gate (ADR-0120 D3/D4).

``compare_experiments`` aligns two finalized experiments **per item** (by
``item_id``) and delegates the verdict entirely to the shipped ADR-0080 gate:
boolean per-item scores become the ``1``/``0`` Bernoulli sequences that
:func:`~novafabric.eval.regression_diff.significance_diff` already consumes;
numeric metrics flow to its existing drift/mean-shift block. **No new statistics
are invented** — the embedded ``significance`` block is the unchanged
:class:`~novafabric.eval.regression_diff.SignificanceDiff`, and the exit-code
contract is inherited verbatim (``3`` on ``ACCEPT_H1``, else ``0``).

Hard errors, never silent skew (spec §Edge cases): comparing experiments whose
``dataset_ref`` differs raises :class:`DatasetMismatchError`; a metric that is
boolean on one side and numeric on the other raises
:class:`MetricTypeMismatchError`. Items present on one side only, errored items,
and items without a score for the metric are reported ``unmatched`` (one side
``null``) and excluded from the SPRT sequences — the SPRT operates only on
paired outcomes.

Wire contract: ``schemas/experiment-comparison.schema.json``. For CI gating the
comparison also renders a ``regression_report``-shaped dict
(:meth:`ExperimentComparison.to_policy_regression_report`) that drops straight
into ``PolicyResource.regression_report`` for the existing Rego regression gate
(ADR-0003/0019) — no new gate engine.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from novafabric.eval.experiment import (
    DatasetRef,
    Experiment,
    ExperimentError,
    ItemRun,
    ItemRunStatus,
)
from novafabric.eval.regression_diff import (
    DEFAULT_ALPHA,
    DEFAULT_BETA,
    DEFAULT_P0,
    DEFAULT_P1,
    significance_diff,
)
from novafabric.eval.scores import SCORES_FILENAME, ScoreValueType, read_scores

EXPERIMENT_COMPARISON_SCHEMA_VERSION: Literal["0.1.0"] = "0.1.0"


class DatasetMismatchError(ExperimentError):
    """The two experiments did not run the same pinned dataset (hard error)."""


class MetricTypeMismatchError(ExperimentError):
    """The metric is boolean on one side and numeric on the other (hard error)."""


class ComparisonOf(BaseModel):
    """The experiment pair a comparison is over."""

    model_config = ConfigDict(extra="forbid")

    baseline_experiment_id: str
    candidate_experiment_id: str


class ComparisonItem(BaseModel):
    """One ``item_id``'s aligned values (``None`` side ⇒ unmatched)."""

    model_config = ConfigDict(extra="forbid")

    item_id: str = Field(min_length=1)
    baseline: float | None = None
    candidate: float | None = None
    changed: bool = False


class ExperimentComparison(BaseModel):
    """Result of ``nova experiment compare`` (spec §Comparison result).

    ``significance`` is the ADR-0080 :class:`SignificanceDiff` embedded
    **verbatim** (as its JSON dump) — this record neither redefines nor extends
    the statistics.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["0.1.0"] = EXPERIMENT_COMPARISON_SCHEMA_VERSION
    comparison_of: ComparisonOf
    dataset_ref: DatasetRef
    metric: str = Field(min_length=1)
    per_item: list[ComparisonItem]
    significance: dict[str, Any]
    exit_code: Literal[0, 3]
    created_at: str

    def is_regression(self) -> bool:
        """True iff the embedded ADR-0080 verdict is a significant regression."""
        return self.exit_code != 0

    def to_policy_regression_report(self) -> dict[str, Any]:
        """A ``regression_report``-shaped gate input (ADR-0003/0019, D4).

        Drops into ``PolicyResource.regression_report`` so the **existing**
        default Rego regression gate (which reads
        ``regression_report.regression_detected``) can fail a promotion on a
        statistically significant experiment regression — no new gate engine.
        """
        sprt = self.significance.get("sprt", {})
        verdict = sprt.get("verdict", "")
        return {
            "regression_detected": self.is_regression(),
            "summary": (
                f"experiment comparison on metric {self.metric!r}: "
                f"SPRT verdict {verdict!r} "
                f"({self.comparison_of.baseline_experiment_id} -> "
                f"{self.comparison_of.candidate_experiment_id})"
            ),
            "metric": self.metric,
            "verdict": verdict,
            "comparison_of": self.comparison_of.model_dump(),
            "significance": self.significance,
        }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _item_values(
    experiment: Experiment, metric: str
) -> tuple[dict[str, float], ScoreValueType | None]:
    """Per-item value for *metric* from one experiment's capsule score logs.

    Only ``status: ok`` items with at least one referenced score named *metric*
    contribute (errored/scoreless items become ``unmatched``). Returns the value
    map and the metric's :class:`ScoreValueType` on this side (``None`` when no
    item carries the metric).
    """
    values: dict[str, float] = {}
    value_type: ScoreValueType | None = None
    for run in experiment.runs:
        value, vt = _run_value(run, metric)
        if value is None or vt is None:
            continue
        if value_type is None:
            value_type = vt
        elif vt is not value_type:
            raise MetricTypeMismatchError(
                f"metric {metric!r} mixes {value_type.value} and {vt.value} scores "
                f"within experiment {experiment.experiment_id}"
            )
        values[run.item_id] = value
    return values, value_type


def _run_value(run: ItemRun, metric: str) -> tuple[float | None, ScoreValueType | None]:
    if run.status is not ItemRunStatus.OK or not run.score_ids:
        return None, None
    scores_path = Path(run.capsule_ref) / SCORES_FILENAME
    wanted = set(run.score_ids)
    for score in read_scores(scores_path):
        if score.score_id not in wanted or score.name != metric:
            continue
        if score.value_type is ScoreValueType.BOOLEAN:
            return (1.0 if score.value else 0.0), ScoreValueType.BOOLEAN
        if score.value_type is ScoreValueType.NUMERIC:
            return float(score.value), ScoreValueType.NUMERIC
        raise MetricTypeMismatchError(
            f"metric {metric!r} is categorical in {scores_path}; "
            "only boolean/numeric metrics are comparable"
        )
    return None, None


def compare_experiments(
    baseline: Experiment,
    candidate: Experiment,
    *,
    metric: str,
    p0: float = DEFAULT_P0,
    p1: float = DEFAULT_P1,
    alpha: float = DEFAULT_ALPHA,
    beta: float = DEFAULT_BETA,
    confidence: float = 0.95,
) -> ExperimentComparison:
    """Diff two experiments per item and in aggregate (ADR-0120 D3).

    Raises :class:`DatasetMismatchError` when the pinned ``dataset_ref`` differs
    on any field, and :class:`MetricTypeMismatchError` on a boolean/numeric
    disagreement. The verdict is produced verbatim by ADR-0080's
    ``significance_diff``; ``exit_code`` follows its contract.
    """
    if baseline.dataset_ref != candidate.dataset_ref:
        raise DatasetMismatchError(
            "experiments did not run the same pinned dataset: "
            f"baseline {baseline.dataset_ref.model_dump()} vs "
            f"candidate {candidate.dataset_ref.model_dump()}"
        )

    base_values, base_type = _item_values(baseline, metric)
    cand_values, cand_type = _item_values(candidate, metric)
    if base_type is not None and cand_type is not None and base_type is not cand_type:
        raise MetricTypeMismatchError(
            f"metric {metric!r} is {base_type.value} in the baseline but "
            f"{cand_type.value} in the candidate"
        )
    value_type = base_type or cand_type

    # Stable alignment order: baseline run order, then candidate-only items.
    ordered_ids = [run.item_id for run in baseline.runs]
    seen = set(ordered_ids)
    for run in candidate.runs:
        if run.item_id not in seen:
            seen.add(run.item_id)
            ordered_ids.append(run.item_id)

    per_item: list[ComparisonItem] = []
    paired_base: list[float] = []
    paired_cand: list[float] = []
    for item_id in ordered_ids:
        b = base_values.get(item_id)
        c = cand_values.get(item_id)
        if b is not None and c is not None:
            paired_base.append(b)
            paired_cand.append(c)
            per_item.append(
                ComparisonItem(item_id=item_id, baseline=b, candidate=c, changed=b != c)
            )
        else:
            per_item.append(ComparisonItem(item_id=item_id, baseline=b, candidate=c))

    if value_type is ScoreValueType.NUMERIC:
        diff = significance_diff(
            [],
            [],
            metric=metric,
            p0=p0,
            p1=p1,
            alpha=alpha,
            beta=beta,
            confidence=confidence,
            numeric_baseline=paired_base,
            numeric_candidate=paired_cand,
        )
    else:
        diff = significance_diff(
            [int(v) for v in paired_base],
            [int(v) for v in paired_cand],
            metric=metric,
            p0=p0,
            p1=p1,
            alpha=alpha,
            beta=beta,
            confidence=confidence,
        )

    exit_code: Literal[0, 3] = 3 if diff.exit_code() == 3 else 0
    return ExperimentComparison(
        comparison_of=ComparisonOf(
            baseline_experiment_id=baseline.experiment_id,
            candidate_experiment_id=candidate.experiment_id,
        ),
        dataset_ref=baseline.dataset_ref,
        metric=metric,
        per_item=per_item,
        significance=diff.model_dump(mode="json"),
        exit_code=exit_code,
        created_at=_now_iso(),
    )
