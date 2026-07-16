"""Per-item experiment runner over a stub command (ADR-0120 D2).

Each dataset item becomes one real Run Capsule (existing capture path); the
ADR-0108 provenance facet is written per capsule; exact-match ``code`` scores
are appended to each capsule's ``scores.jsonl``; the finalized record binds the
dataset content hash. Errored items stay in the record but out of the
aggregate. Zero model calls anywhere.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from novafabric.eval.dataset_provenance import read_facets
from novafabric.eval.experiment import ExperimentError, ExperimentTarget, TargetKind
from novafabric.eval.experiment_dataset import DatasetItem, load_dataset
from novafabric.eval.experiment_runner import (
    compute_aggregates,
    render_command,
    run_experiment,
)
from novafabric.eval.scores import SCORES_FILENAME, Score, ScoreSource, ScoreValueType, read_scores

_TARGET = ExperimentTarget(kind=TargetKind.AGENT, ref="stub-agent@1.0.0")
_ECHO = [sys.executable, "-c", "print('{input}')"]


def _dataset(tmp_path: Path, rows: list[dict[str, object]]) -> Path:
    path = tmp_path / "items.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return path


def test_render_command_substitutes_placeholders() -> None:
    item = DatasetItem(item_id="i-1", input="hello", expected="world")
    assert render_command(["run", "--q", "{input}", "{item_id}", "{expected}"], item) == [
        "run",
        "--q",
        "hello",
        "i-1",
        "world",
    ]
    # no placeholders -> command unchanged; absent expected -> empty string
    bare = DatasetItem(item_id="i-2", input="x")
    assert render_command(["agent.py", "{expected}"], bare) == ["agent.py", ""]


def test_run_experiment_produces_capsule_scores_and_bound_record(tmp_path: Path) -> None:
    ds = load_dataset(
        _dataset(
            tmp_path,
            [
                {"item_id": "a", "input": "hello", "expected": "hello"},  # pass
                {"item_id": "b", "input": "world", "expected": "nope"},  # fail
            ],
        )
    )
    experiment = run_experiment(ds, _ECHO, target=_TARGET, runs_dir=tmp_path / "runs")

    # finalized, content-addressed, and bound to the pinned dataset hashes
    assert experiment.status == "finalized"
    assert experiment.content_hash is not None
    assert experiment.dataset_ref == ds.dataset_ref
    assert experiment.target == _TARGET

    assert [r.item_id for r in experiment.runs] == ["a", "b"]
    for run in experiment.runs:
        assert run.status.value == "ok"
        capsule_dir = Path(run.capsule_ref)
        assert (capsule_dir / "capsule.yaml").exists()
        # ADR-0108 facet written per item capsule (contamination checks keep working)
        facets = read_facets(capsule_dir)
        assert len(facets) == 1
        assert facets[0].dataset_hash == ds.dataset_ref.dataset_hash
        assert facets[0].split_hash == ds.dataset_ref.split_hash
        # one zero-token code score per item, referenced by id
        scores = read_scores(capsule_dir / SCORES_FILENAME)
        assert len(scores) == 1
        assert scores[0].source is ScoreSource.CODE
        assert run.score_ids == [scores[0].score_id]

    # aggregate: 1 pass / 2 items with a Wilson band
    assert len(experiment.aggregate) == 1
    agg = experiment.aggregate[0]
    assert agg.metric == "exact_match"
    assert agg.reducer == "pass_rate"
    assert agg.value == 0.5
    assert agg.n == 2
    assert agg.wilson is not None


def test_errored_item_recorded_but_excluded_from_aggregate(tmp_path: Path) -> None:
    ds = load_dataset(
        _dataset(
            tmp_path,
            [
                {"item_id": "ok", "input": "hi", "expected": "hi"},
                {"item_id": "boom", "input": "x", "expected": "x"},
            ],
        )
    )
    command = [sys.executable, "-c", "import sys; sys.exit(1) if '{item_id}' == 'boom' else print('{input}')"]
    experiment = run_experiment(ds, command, target=_TARGET, runs_dir=tmp_path / "runs")
    by_id = {r.item_id: r for r in experiment.runs}
    assert by_id["ok"].status.value == "ok"
    assert by_id["boom"].status.value == "error"
    assert by_id["boom"].score_ids == []
    assert experiment.aggregate[0].n == 1  # errored item contributes nothing
    assert experiment.status == "finalized"  # partial results still finalize


def test_unspawnable_command_is_an_error_item_not_a_crash(tmp_path: Path) -> None:
    ds = load_dataset(_dataset(tmp_path, [{"item_id": "a", "input": "x", "expected": "x"}]))
    experiment = run_experiment(
        ds, ["/nonexistent-binary-adr0120"], target=_TARGET, runs_dir=tmp_path / "runs"
    )
    assert experiment.runs[0].status.value == "error"
    assert experiment.aggregate == []
    assert experiment.status == "finalized"


def test_rerun_mints_a_new_experiment_id(tmp_path: Path) -> None:
    ds = load_dataset(_dataset(tmp_path, [{"item_id": "a", "input": "hi", "expected": "hi"}]))
    first = run_experiment(ds, _ECHO, target=_TARGET, runs_dir=tmp_path / "runs")
    second = run_experiment(ds, _ECHO, target=_TARGET, runs_dir=tmp_path / "runs")
    assert first.experiment_id != second.experiment_id  # D1: a re-run is a new experiment
    assert first.dataset_ref == second.dataset_ref


def test_item_without_expected_gets_no_score(tmp_path: Path) -> None:
    ds = load_dataset(_dataset(tmp_path, [{"item_id": "a", "input": "hi"}]))
    experiment = run_experiment(ds, _ECHO, target=_TARGET, runs_dir=tmp_path / "runs")
    assert experiment.runs[0].status.value == "ok"
    assert experiment.runs[0].score_ids == []
    assert experiment.aggregate == []


# ── compute_aggregates (pure) ────────────────────────────────────────────────

_DIGEST = "sha256:" + "9" * 64


def _score(name: str, value: bool | float | str, value_type: ScoreValueType) -> Score:
    return Score(
        subject=_DIGEST,
        name=name,
        value=value,
        value_type=value_type,
        source=ScoreSource.CODE,
        evaluator_id="ev",
        eval_card_digest=_DIGEST,
    )


def test_compute_aggregates_boolean_and_numeric() -> None:
    scores = [
        _score("pass", True, ScoreValueType.BOOLEAN),
        _score("pass", False, ScoreValueType.BOOLEAN),
        _score("latency", 2.0, ScoreValueType.NUMERIC),
        _score("latency", 4.0, ScoreValueType.NUMERIC),
        _score("grade", "A", ScoreValueType.CATEGORICAL),  # skipped: no v0 reducer
    ]
    aggregates = {a.metric: a for a in compute_aggregates(scores)}
    assert set(aggregates) == {"pass", "latency"}
    assert aggregates["pass"].reducer == "pass_rate"
    assert aggregates["pass"].value == 0.5
    assert aggregates["pass"].wilson is not None
    assert aggregates["latency"].reducer == "mean"
    assert aggregates["latency"].value == 3.0
    assert aggregates["latency"].wilson is None


def test_compute_aggregates_rejects_mixed_types() -> None:
    scores = [
        _score("m", True, ScoreValueType.BOOLEAN),
        _score("m", 0.5, ScoreValueType.NUMERIC),
    ]
    with pytest.raises(ExperimentError, match="mixes"):
        compute_aggregates(scores)
