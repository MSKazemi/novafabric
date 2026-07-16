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

"""Per-item dataset-experiment runner (ADR-0120 D2).

Iterates a pinned dataset, executes the target command once per item through the
**existing** :class:`~novafabric.capture.orchestrator.CaptureOrchestrator` (one
Run Capsule per item — the capsule invariant is untouched), writes the ADR-0108
dataset-provenance facet into each item capsule (so per-item contamination checks
keep working), scores each capsule with the built-in **zero-token** exact-match
``code`` scorer (ADR-0099 — no model call, ever), and returns a finalized,
content-hashed :class:`~novafabric.eval.experiment.Experiment`.

Command templating: any argument containing ``{input}``, ``{item_id}``, or
``{expected}`` has the placeholder substituted with the item's value before
capture. A command with no placeholders simply runs once per item unchanged.

Errored items (non-zero exit or a failed spawn) are recorded with
``status: error``, excluded from scoring and from every aggregate — the
experiment still finalizes; partial results are honest evidence, not a hard
failure (spec §Edge cases).
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

from novafabric.eval.card import EvalCard, card_digest
from novafabric.eval.dataset_provenance import DatasetProvenanceFacet, write_facet
from novafabric.eval.experiment import (
    Experiment,
    ExperimentError,
    ExperimentTarget,
    ItemRun,
    ItemRunStatus,
    MetricAggregate,
    finalize_experiment,
)
from novafabric.eval.experiment_dataset import DatasetItem, LoadedDataset
from novafabric.eval.scores import (
    SCORES_FILENAME,
    Score,
    ScoreSource,
    ScoreValueType,
    append_score,
)
from novafabric.eval.significance import wilson_interval

#: Default boolean metric emitted by the built-in exact-match scorer.
DEFAULT_METRIC = "exact_match"

#: Built-in ``code`` evaluator card for the offline exact-match scorer. Like the
#: NF-009 offline cards it carries no signature; its digest is the
#: reproducibility key stamped onto every emitted score.
EXACT_MATCH_CARD = EvalCard(
    card_id="nf-experiment-exact-match",
    name="Experiment Exact Match",
    version="1.0.0",
    source=ScoreSource.CODE,
)


def render_command(command: Sequence[str], item: DatasetItem) -> list[str]:
    """Substitute ``{input}``/``{item_id}``/``{expected}`` placeholders per item."""
    rendered: list[str] = []
    for arg in command:
        arg = arg.replace("{input}", item.input_text())
        arg = arg.replace("{item_id}", item.item_id)
        arg = arg.replace("{expected}", item.expected or "")
        rendered.append(arg)
    return rendered


def _capsule_stdout(capsule_dir: Path) -> str:
    path = capsule_dir / "outputs" / "stdout.txt"
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def _score_exact_match(capsule_dir: Path, run_id: str, item: DatasetItem, metric: str) -> Score:
    """Boolean exact-match Score over the capsule's recorded stdout (zero-token)."""
    from novafabric.evidence.merkle import capsule_merkle_root

    passed = _capsule_stdout(capsule_dir).strip() == (item.expected or "").strip()
    return Score(
        subject=capsule_merkle_root(capsule_dir),
        subject_kind="capsule",
        name=metric,
        value=passed,
        value_type=ScoreValueType.BOOLEAN,
        source=ScoreSource.CODE,
        evaluator_id=EXACT_MATCH_CARD.card_id,
        eval_card_digest=card_digest(EXACT_MATCH_CARD),
        run_id=run_id,
    )


def compute_aggregates(
    scores: Sequence[Score], confidence: float = 0.95
) -> list[MetricAggregate]:
    """Per-metric aggregates over item scores (spec §MetricAggregate).

    Boolean metrics reduce to ``pass_rate`` with an ADR-0080 Wilson band;
    numeric metrics reduce to ``mean`` (``wilson: null``). Categorical scores
    are skipped (no v0 reducer). A metric name carrying both boolean and
    numeric scores is an error — the aggregate would be meaningless.
    """
    by_metric: dict[str, list[Score]] = {}
    for score in scores:
        by_metric.setdefault(score.name, []).append(score)

    aggregates: list[MetricAggregate] = []
    for metric in sorted(by_metric):
        group = [s for s in by_metric[metric] if s.value_type is not ScoreValueType.CATEGORICAL]
        if not group:
            continue
        value_types = {s.value_type for s in group}
        if len(value_types) > 1:
            raise ExperimentError(
                f"metric {metric!r} mixes boolean and numeric scores; cannot aggregate"
            )
        if group[0].value_type is ScoreValueType.BOOLEAN:
            successes = sum(1 for s in group if s.value)
            n = len(group)
            aggregates.append(
                MetricAggregate(
                    metric=metric,
                    value_type="boolean",
                    reducer="pass_rate",
                    value=successes / n,
                    n=n,
                    wilson=wilson_interval(successes, n, confidence),
                )
            )
        else:
            values = [float(s.value) for s in group]
            aggregates.append(
                MetricAggregate(
                    metric=metric,
                    value_type="numeric",
                    reducer="mean",
                    value=sum(values) / len(values),
                    n=len(values),
                    wilson=None,
                )
            )
    return aggregates


def run_experiment(
    dataset: LoadedDataset,
    command: Sequence[str],
    *,
    target: ExperimentTarget,
    metric: str = DEFAULT_METRIC,
    runs_dir: Path | None = None,
    score_config_ref: str | None = None,
    labels: dict[str, str] | None = None,
    baseline_experiment_id: str | None = None,
    timeout_s: float | None = None,
) -> Experiment:
    """Run *command* across every dataset item; return a finalized record (D2).

    One Run Capsule per item via the existing capture orchestrator; the ADR-0108
    provenance facet is written into each capsule; items with an ``expected``
    value get a boolean exact-match ``code`` score appended to the capsule's
    ``scores.jsonl`` (ADR-0099, additive). Zero-token by construction.
    """
    from novafabric.capture.orchestrator import CaptureOrchestrator

    orchestrator = CaptureOrchestrator(base_dir=runs_dir)
    facet_base = DatasetProvenanceFacet(
        name=dataset.dataset_ref.name,
        version=dataset.dataset_ref.version,
        dataset_hash=dataset.dataset_ref.dataset_hash,
        split_hash=dataset.dataset_ref.split_hash,
    )

    runs: list[ItemRun] = []
    collected: list[Score] = []
    for item in dataset.items:
        try:
            result = orchestrator.run(
                command=render_command(command, item), timeout_s=timeout_s
            )
        except Exception:  # noqa: BLE001 — an unspawnable item is evidence, not a crash
            runs.append(
                ItemRun(
                    item_id=item.item_id,
                    capsule_ref="unavailable",
                    score_ids=[],
                    status=ItemRunStatus.ERROR,
                )
            )
            continue

        write_facet(result.capsule_dir, facet_base)
        if result.exit_code != 0:
            runs.append(
                ItemRun(
                    item_id=item.item_id,
                    capsule_ref=str(result.capsule_dir),
                    score_ids=[],
                    status=ItemRunStatus.ERROR,
                )
            )
            continue

        score_ids: list[str] = []
        if item.expected is not None:
            score = _score_exact_match(result.capsule_dir, result.run_id, item, metric)
            append_score(result.capsule_dir / SCORES_FILENAME, score)
            collected.append(score)
            score_ids.append(score.score_id)
        runs.append(
            ItemRun(
                item_id=item.item_id,
                capsule_ref=str(result.capsule_dir),
                score_ids=score_ids,
                status=ItemRunStatus.OK,
            )
        )

    experiment = Experiment(
        dataset_ref=dataset.dataset_ref,
        target=target,
        runs=runs,
        aggregate=compute_aggregates(collected),
        status="running",
        score_config_ref=score_config_ref,
        baseline_experiment_id=baseline_experiment_id,
        labels=labels or {},
    )
    return finalize_experiment(experiment)
