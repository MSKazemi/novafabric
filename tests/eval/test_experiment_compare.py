"""A/B experiment comparison + gate delegation (ADR-0120 D3/D4).

Synthetic capsule dirs (a ``scores.jsonl`` each) keep these fast — the verdict
must come verbatim from the shipped ADR-0080 ``significance_diff``, the exit
code must follow its contract (3 on ``ACCEPT_H1``), mismatched datasets and
boolean/numeric disagreements must hard-error, and unmatched/errored items must
stay out of the SPRT sequences. Also covers the Rego-gate input shape
(``PolicyResource.regression_report``).
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from novafabric.eval.experiment import (
    DatasetRef,
    Experiment,
    ExperimentTarget,
    ItemRun,
    ItemRunStatus,
    TargetKind,
    finalize_experiment,
)
from novafabric.eval.experiment_compare import (
    DatasetMismatchError,
    MetricTypeMismatchError,
    compare_experiments,
)
from novafabric.eval.scores import (
    SCORES_FILENAME,
    Score,
    ScoreSource,
    ScoreValueType,
    append_score,
)
from novafabric.policy._models import PolicyResource

_DIGEST = "sha256:" + "7" * 64
_DATASET = DatasetRef(
    name="ds", version="1", dataset_hash="sha256:" + "a" * 64, split_hash="sha256:" + "b" * 64
)
_TARGET = ExperimentTarget(kind=TargetKind.AGENT, ref="agent@1.0")
_SCHEMA = Path(__file__).parents[2] / "schemas" / "experiment-comparison.schema.json"


def _make_experiment(
    tmp_path: Path,
    tag: str,
    items: dict[str, object],
    *,
    metric: str = "exact_match",
    dataset: DatasetRef = _DATASET,
) -> Experiment:
    """Build a finalized experiment whose items carry the given metric values.

    Values: ``True``/``False`` (boolean score), a float (numeric score),
    ``None`` (ok item, no scores), or ``"error"`` (errored item).
    """
    runs: list[ItemRun] = []
    for item_id, value in items.items():
        capsule_dir = tmp_path / tag / item_id
        capsule_dir.mkdir(parents=True)
        if value == "error":
            runs.append(
                ItemRun(
                    item_id=item_id,
                    capsule_ref=str(capsule_dir),
                    status=ItemRunStatus.ERROR,
                )
            )
            continue
        score_ids: list[str] = []
        if value is not None:
            value_type = (
                ScoreValueType.BOOLEAN if isinstance(value, bool) else ScoreValueType.NUMERIC
            )
            if isinstance(value, str):
                value_type = ScoreValueType.CATEGORICAL
            score = Score(
                subject=_DIGEST,
                name=metric,
                value=value,
                value_type=value_type,
                source=ScoreSource.CODE,
                evaluator_id="ev",
                eval_card_digest=_DIGEST,
            )
            append_score(capsule_dir / SCORES_FILENAME, score)
            score_ids.append(score.score_id)
        runs.append(ItemRun(item_id=item_id, capsule_ref=str(capsule_dir), score_ids=score_ids))
    return finalize_experiment(
        Experiment(
            dataset_ref=dataset, target=_TARGET, runs=runs, aggregate=[], status="running"
        )
    )


def test_regression_detected_exit_3(tmp_path: Path) -> None:
    items = {f"i{k}": True for k in range(5)}
    baseline = _make_experiment(tmp_path, "base", items)
    candidate = _make_experiment(tmp_path, "cand", dict.fromkeys(items, False))
    comparison = compare_experiments(baseline, candidate, metric="exact_match")
    assert comparison.significance["sprt"]["verdict"] == "accept_h1"
    assert comparison.exit_code == 3
    assert comparison.is_regression()
    assert all(item.changed for item in comparison.per_item)


def test_no_change_exit_0(tmp_path: Path) -> None:
    items = {f"i{k}": True for k in range(12)}
    baseline = _make_experiment(tmp_path, "base", items)
    candidate = _make_experiment(tmp_path, "cand", items)
    comparison = compare_experiments(baseline, candidate, metric="exact_match")
    assert comparison.significance["sprt"]["verdict"] == "accept_h0"
    assert comparison.exit_code == 0
    assert not any(item.changed for item in comparison.per_item)


def test_improvement_exit_0(tmp_path: Path) -> None:
    items = {f"i{k}": False for k in range(12)}
    baseline = _make_experiment(tmp_path, "base", items)
    candidate = _make_experiment(tmp_path, "cand", dict.fromkeys(items, True))
    comparison = compare_experiments(baseline, candidate, metric="exact_match")
    assert comparison.exit_code == 0
    assert not comparison.is_regression()
    assert all(item.changed for item in comparison.per_item)


def test_dataset_mismatch_is_a_hard_error(tmp_path: Path) -> None:
    other = DatasetRef(
        name="ds",
        version="2",
        dataset_hash="sha256:" + "c" * 64,
        split_hash="sha256:" + "d" * 64,
    )
    baseline = _make_experiment(tmp_path, "base", {"a": True})
    candidate = _make_experiment(tmp_path, "cand", {"a": True}, dataset=other)
    with pytest.raises(DatasetMismatchError):
        compare_experiments(baseline, candidate, metric="exact_match")


def test_boolean_numeric_mismatch_across_sides_errors(tmp_path: Path) -> None:
    baseline = _make_experiment(tmp_path, "base", {"a": True, "b": False})
    candidate = _make_experiment(tmp_path, "cand", {"a": 0.5, "b": 0.7})
    with pytest.raises(MetricTypeMismatchError, match="boolean.*numeric"):
        compare_experiments(baseline, candidate, metric="exact_match")


def test_categorical_metric_not_comparable(tmp_path: Path) -> None:
    baseline = _make_experiment(tmp_path, "base", {"a": "good"})
    candidate = _make_experiment(tmp_path, "cand", {"a": "bad"})
    with pytest.raises(MetricTypeMismatchError, match="categorical"):
        compare_experiments(baseline, candidate, metric="exact_match")


def test_unmatched_and_errored_items_excluded_from_sprt(tmp_path: Path) -> None:
    baseline = _make_experiment(
        tmp_path,
        "base",
        {"a": True, "b": True, "only-base": True, "err": True, "noscore": True},
    )
    candidate = _make_experiment(
        tmp_path,
        "cand",
        {"a": True, "b": False, "err": "error", "noscore": None, "only-cand": True},
    )
    comparison = compare_experiments(baseline, candidate, metric="exact_match")
    per_item = {item.item_id: item for item in comparison.per_item}
    assert set(per_item) == {"a", "b", "only-base", "err", "noscore", "only-cand"}
    # only the two paired items enter the Bernoulli sequences
    assert comparison.significance["baseline"]["n"] == 2
    assert comparison.significance["candidate"]["n"] == 2
    for unmatched in ("only-base", "err", "noscore", "only-cand"):
        item = per_item[unmatched]
        assert item.baseline is None or item.candidate is None
        assert item.changed is False


def test_numeric_metric_uses_drift_block(tmp_path: Path) -> None:
    base_values = {f"i{k}": 1.0 + 0.1 * k for k in range(4)}
    baseline = _make_experiment(tmp_path, "base", base_values, metric="latency")
    candidate = _make_experiment(
        tmp_path, "cand", {k: v + 5.0 for k, v in base_values.items()}, metric="latency"
    )
    comparison = compare_experiments(baseline, candidate, metric="latency")
    # SPRT ran over empty boolean sequences -> inconclusive, never a regression
    assert comparison.significance["sprt"]["verdict"] == "continue"
    assert comparison.exit_code == 0
    drift = comparison.significance["drift"]
    assert drift["detected"] is True  # drift != regression: reported separately
    assert comparison.significance["numeric"]["candidate_mean"] > 5.0
    assert all(item.changed for item in comparison.per_item)


def test_comparison_matches_graduated_schema(tmp_path: Path) -> None:
    baseline = _make_experiment(tmp_path, "base", {"a": True, "b": False})
    candidate = _make_experiment(tmp_path, "cand", {"a": True, "b": True})
    comparison = compare_experiments(baseline, candidate, metric="exact_match")
    schema = json.loads(_SCHEMA.read_text())
    validator = jsonschema.Draft202012Validator(
        schema, format_checker=jsonschema.FormatChecker()
    )
    document = comparison.model_dump(mode="json")
    errors = list(validator.iter_errors(document))
    assert errors == [], [e.message for e in errors]


def test_gate_input_shape_feeds_policy_resource(tmp_path: Path) -> None:
    """D4: the comparison drops into PolicyResource.regression_report unchanged."""
    items = {f"i{k}": True for k in range(5)}
    baseline = _make_experiment(tmp_path, "base", items)
    candidate = _make_experiment(tmp_path, "cand", dict.fromkeys(items, False))
    comparison = compare_experiments(baseline, candidate, metric="exact_match")

    report = comparison.to_policy_regression_report()
    # the default Rego regression gate reads regression_report.regression_detected
    assert report["regression_detected"] is True
    resource = PolicyResource(kind="asset", ref="agent@1.0", regression_report=report)
    assert resource.regression_report is not None
    assert resource.regression_report["regression_detected"] is True
    assert resource.regression_report["significance"]["sprt"]["verdict"] == "accept_h1"

    improved = compare_experiments(baseline, baseline, metric="exact_match")
    assert improved.to_policy_regression_report()["regression_detected"] is False
