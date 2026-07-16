"""``Experiment`` record model, content hash, and file store (ADR-0120 D1).

The 14 golden fixtures under ``design/spec/fixtures/dataset-experiment/`` must
behave as their filename asserts against the graduated ``/schemas/`` copies
(the graduation changed identity metadata only). Model tests cover the D1
immutability invariants the schema cannot express: tamper-evident
``content_hash``, finalize-once, and a store that never overwrites.
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest
from pydantic import ValidationError

from novafabric.eval.experiment import (
    DatasetRef,
    Experiment,
    ExperimentError,
    ExperimentExistsError,
    ExperimentFinalizedError,
    ExperimentNotFoundError,
    ExperimentTarget,
    ItemRun,
    MetricAggregate,
    TargetKind,
    default_experiments_dir,
    experiment_content_hash,
    finalize_experiment,
    list_experiments,
    load_experiment,
    save_experiment,
)

_ROOT = Path(__file__).parents[2]
_FIXTURES = _ROOT / "design" / "spec" / "fixtures" / "dataset-experiment"
_FIXTURE_FILES = sorted(_FIXTURES.glob("*.json"))

_HASH_A = "sha256:" + "a" * 64
_HASH_B = "sha256:" + "b" * 64


def _validator(schema_name: str) -> jsonschema.Draft202012Validator:
    schema = json.loads((_ROOT / "schemas" / schema_name).read_text())
    return jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())


def test_fixture_corpus_is_complete() -> None:
    names = [p.name for p in _FIXTURE_FILES]
    assert len(names) == 14
    assert sum(n.startswith("experiment-valid") for n in names) == 3
    assert sum(n.startswith("experiment-invalid") for n in names) == 6
    assert sum(n.startswith("comparison-valid") for n in names) == 2
    assert sum(n.startswith("comparison-invalid") for n in names) == 3


@pytest.mark.parametrize("fixture", _FIXTURE_FILES, ids=lambda p: p.stem)
def test_fixture_against_graduated_schema(fixture: Path) -> None:
    schema_name = (
        "experiment.schema.json"
        if fixture.name.startswith("experiment-")
        else "experiment-comparison.schema.json"
    )
    document = json.loads(fixture.read_text())
    errors = list(_validator(schema_name).iter_errors(document))
    if "-valid" in fixture.name:
        assert errors == [], f"{fixture.name}: {[e.message for e in errors]}"
    else:
        assert errors, f"{fixture.name} unexpectedly passed schema validation"


def test_running_fixture_parses_as_model() -> None:
    document = json.loads((_FIXTURES / "experiment-valid-running.json").read_text())
    experiment = Experiment.model_validate(document)
    assert experiment.status == "running"
    assert experiment.content_hash is None


# ── Model construction ────────────────────────────────────────────────────────


def _dataset_ref() -> DatasetRef:
    return DatasetRef(name="ds", version="1", dataset_hash=_HASH_A, split_hash=_HASH_B)


def _running(**overrides: object) -> Experiment:
    base: dict[str, object] = {
        "dataset_ref": _dataset_ref(),
        "target": ExperimentTarget(kind=TargetKind.AGENT, ref="agent@1.0"),
        "runs": [ItemRun(item_id="i1", capsule_ref="capsules/x", score_ids=[])],
        "aggregate": [],
        "status": "running",
    }
    base.update(overrides)
    return Experiment.model_validate(base)


def test_bad_experiment_id_rejected() -> None:
    with pytest.raises(ValidationError, match="ULID"):
        _running(experiment_id="not-a-ulid")


def test_bad_dataset_hash_rejected() -> None:
    with pytest.raises(ValidationError, match="sha256"):
        DatasetRef(name="ds", version="1", dataset_hash="deadbeef", split_hash=_HASH_B)


def test_bad_score_id_rejected() -> None:
    with pytest.raises(ValidationError, match="ULID"):
        ItemRun(item_id="i1", capsule_ref="c", score_ids=["nope"])


def test_running_must_not_carry_content_hash() -> None:
    with pytest.raises(ValidationError, match="running"):
        _running(content_hash=_HASH_A)


def test_finalized_requires_hash_and_timestamp() -> None:
    with pytest.raises(ValidationError, match="finalized"):
        _running(status="finalized")


def test_unknown_key_rejected() -> None:
    with pytest.raises(ValidationError):
        Experiment.model_validate({**_running().model_dump(mode="json"), "surprise": 1})


# ── Finalize + content hash (D1) ─────────────────────────────────────────────


def test_finalize_produces_valid_hashed_record() -> None:
    running = _running()
    final = finalize_experiment(running)
    assert final.status == "finalized"
    assert final.finalized_at is not None
    assert final.content_hash == experiment_content_hash(final)
    # the input record is untouched (a new record is returned)
    assert running.status == "running"
    assert running.content_hash is None


def test_finalized_record_matches_graduated_schema() -> None:
    """Round-trip contract: what the code writes validates against the schema."""
    final = finalize_experiment(
        _running(
            aggregate=[
                MetricAggregate(
                    metric="exact_match",
                    value_type="boolean",
                    reducer="pass_rate",
                    value=0.5,
                    n=2,
                    wilson=(0.1, 0.9),
                )
            ]
        )
    )
    document = final.model_dump(mode="json", exclude_none=True)
    errors = list(_validator("experiment.schema.json").iter_errors(document))
    assert errors == [], [e.message for e in errors]


def test_finalize_twice_is_an_error() -> None:
    final = finalize_experiment(_running())
    with pytest.raises(ExperimentFinalizedError):
        finalize_experiment(final)


def test_tampered_content_hash_rejected_at_parse() -> None:
    final = finalize_experiment(_running())
    body = final.model_dump(mode="json", exclude_none=True)
    body["target"]["ref"] = "agent@9.9"  # tamper after hashing
    with pytest.raises(ValidationError, match="content_hash"):
        Experiment.model_validate(body)


def test_content_hash_is_deterministic_and_body_sensitive() -> None:
    a = _running(experiment_id="01HXAY7M7QM4YZ2K7N9DPBYK2W", created_at="2026-07-15T00:00:00Z")
    b = _running(experiment_id="01HXAY7M7QM4YZ2K7N9DPBYK2W", created_at="2026-07-15T00:00:00Z")
    assert experiment_content_hash(a) == experiment_content_hash(b)
    c = _running(
        experiment_id="01HXAY7M7QM4YZ2K7N9DPBYK2W",
        created_at="2026-07-15T00:00:00Z",
        labels={"ci": "true"},
    )
    assert experiment_content_hash(a) != experiment_content_hash(c)


# ── File store ────────────────────────────────────────────────────────────────


def test_save_load_roundtrip(tmp_path: Path) -> None:
    final = finalize_experiment(_running())
    path = save_experiment(final, tmp_path)
    assert path.name == f"{final.experiment_id}.json"
    assert load_experiment(final.experiment_id, tmp_path) == final
    assert load_experiment(str(path)) == final  # by explicit path too


def test_save_never_overwrites(tmp_path: Path) -> None:
    final = finalize_experiment(_running())
    save_experiment(final, tmp_path)
    with pytest.raises(ExperimentExistsError):
        save_experiment(final, tmp_path)


def test_load_missing_raises(tmp_path: Path) -> None:
    with pytest.raises(ExperimentNotFoundError):
        load_experiment("01HXAY7M7QM4YZ2K7N9DPBYK2W", tmp_path)


def test_load_corrupt_record_raises(tmp_path: Path) -> None:
    (tmp_path / "01HXAY7M7QM4YZ2K7N9DPBYK2W.json").write_text("{not json")
    with pytest.raises(ExperimentError):
        load_experiment("01HXAY7M7QM4YZ2K7N9DPBYK2W", tmp_path)


def test_list_experiments_sorted_and_empty(tmp_path: Path) -> None:
    assert list_experiments(tmp_path / "absent") == []
    first = finalize_experiment(_running(created_at="2026-07-15T00:00:01+00:00"))
    second = finalize_experiment(_running(created_at="2026-07-15T00:00:02+00:00"))
    save_experiment(second, tmp_path)
    save_experiment(first, tmp_path)
    listed = list_experiments(tmp_path)
    assert [e.experiment_id for e in listed] == [first.experiment_id, second.experiment_id]


def test_default_experiments_dir_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOVAFABRIC_EXPERIMENTS_DIR", "/tmp/exp-store")
    assert default_experiments_dir() == Path("/tmp/exp-store")
    monkeypatch.delenv("NOVAFABRIC_EXPERIMENTS_DIR")
    assert default_experiments_dir() == Path.cwd() / ".novafabric" / "experiments"
