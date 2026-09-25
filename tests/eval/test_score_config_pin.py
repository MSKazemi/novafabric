"""ADR-0117 P4 — score-config aggregation pin + comparability keyed by digest.

Acceptance criteria covered here:

- a ``score_config_ref`` (name / name@version / digest) resolves **read-only**
  through the local catalog to a ``sha256:`` digest recorded on the experiment;
- an unresolvable, malformed, oversize, mismatched-metric, mismatched-type, or
  tampered ref is an explicit :class:`ScoreConfigPinError` raised before any
  item runs — never a silently dropped pin;
- resolution never creates the registry DB or its table (read-only guarantee);
- ``compare`` carries a ``score_config`` block: same digest ⇒ ``true``,
  different ⇒ ``false`` (both digests + message), missing ⇒ ``null``;
- the comparison output (all three states) validates against the graduated
  ``experiment-comparison.schema.json``; records without a pin hash unchanged.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import jsonschema
import pytest

from novafabric.eval.experiment import (
    DatasetRef,
    Experiment,
    ExperimentTarget,
    ItemRun,
    TargetKind,
    experiment_content_hash,
    finalize_experiment,
)
from novafabric.eval.experiment_compare import compare_experiments
from novafabric.eval.experiment_dataset import load_dataset
from novafabric.eval.experiment_runner import run_experiment
from novafabric.eval.score_config import ScoreRange
from novafabric.eval.score_config_catalog import (
    ScoreConfigNotFoundError,
    register_config,
    resolve_config_ref_readonly,
)
from novafabric.eval.score_config_pin import (
    MAX_SCORE_CONFIG_REF_LEN,
    ScoreConfigPinError,
    pinned_ref,
    resolve_score_config_pin,
    score_config_comparability,
)
from novafabric.eval.scores import (
    SCORES_FILENAME,
    Score,
    ScoreSource,
    ScoreValueType,
    append_score,
)

_ROOT = Path(__file__).parents[2]
_DIGEST = "sha256:" + "7" * 64
_DIG_A = "sha256:" + "a" * 64
_DIG_B = "sha256:" + "b" * 64
_DATASET = DatasetRef(
    name="ds", version="1", dataset_hash="sha256:" + "c" * 64, split_hash="sha256:" + "d" * 64
)
_TARGET = ExperimentTarget(kind=TargetKind.AGENT, ref="agent@1.0")
_ECHO = [sys.executable, "-c", "print('{input}')"]


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    """A registry with ``exact_match`` v1 and v2 (boolean) and a numeric metric."""
    path = tmp_path / "registry.db"
    register_config("exact_match", ScoreValueType.BOOLEAN, "strict equality", db_path=path)
    register_config("exact_match", ScoreValueType.BOOLEAN, "stripped equality", db_path=path)
    register_config(
        "latency",
        ScoreValueType.NUMERIC,
        "seconds",
        range_=ScoreRange(min=0, max=60),
        db_path=path,
    )
    return path


def _schema_validator(name: str) -> jsonschema.Draft202012Validator:
    schema = json.loads((_ROOT / "schemas" / name).read_text())
    return jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())


# ── resolution ───────────────────────────────────────────────────────────────


def test_resolve_by_name_version_and_digest(db: Path) -> None:
    latest = resolve_score_config_pin(
        "exact_match", metric="exact_match", value_type=ScoreValueType.BOOLEAN, db_path=db
    )
    assert latest.version == 2
    v1 = resolve_score_config_pin(
        "exact_match@1", metric="exact_match", value_type=ScoreValueType.BOOLEAN, db_path=db
    )
    assert v1.version == 1 and v1.content_digest != latest.content_digest
    by_digest = resolve_score_config_pin(
        v1.content_digest, metric="exact_match", value_type=ScoreValueType.BOOLEAN, db_path=db
    )
    assert by_digest.content_digest == v1.content_digest
    assert pinned_ref(by_digest) == "exact_match@1"


@pytest.mark.parametrize(
    ("ref", "fragment"),
    [
        ("missing", "no score config registered"),
        ("exact_match@9", "no score config registered"),
        ("exact_match@abc", "invalid config ref"),
        ("@1", "invalid config ref"),
        ("sha256:" + "0" * 64, "no score config registered"),
        ("   ", "must not be empty"),
        ("x" * (MAX_SCORE_CONFIG_REF_LEN + 1), "the limit is"),
        ("latency@1", "governs metric 'latency'"),
    ],
)
def test_unresolvable_or_mismatched_ref_is_explicit(db: Path, ref: str, fragment: str) -> None:
    with pytest.raises(ScoreConfigPinError, match=fragment):
        resolve_score_config_pin(
            ref, metric="exact_match", value_type=ScoreValueType.BOOLEAN, db_path=db
        )


def test_value_type_mismatch_is_explicit(db: Path) -> None:
    with pytest.raises(ScoreConfigPinError, match="declares value_type 'numeric'"):
        resolve_score_config_pin(
            "latency", metric="latency", value_type=ScoreValueType.BOOLEAN, db_path=db
        )


def test_missing_registry_is_not_created(tmp_path: Path) -> None:
    path = tmp_path / "nope" / "registry.db"
    with pytest.raises(ScoreConfigPinError, match="does not exist"):
        resolve_score_config_pin(
            "exact_match", metric="exact_match", value_type=ScoreValueType.BOOLEAN, db_path=path
        )
    assert not path.exists() and not path.parent.exists()


def test_registry_without_table_is_not_found_and_untouched(tmp_path: Path) -> None:
    path = tmp_path / "registry.db"
    sqlite3.connect(path).close()  # empty DB, no score_configs table
    with pytest.raises(ScoreConfigNotFoundError):
        resolve_config_ref_readonly("exact_match", db_path=path)
    conn = sqlite3.connect(path)
    tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    conn.close()
    assert tables == []  # read-only: no table was created


def test_tampered_stored_config_fails_closed(db: Path) -> None:
    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT config_json FROM score_configs WHERE name='exact_match' AND version=1"
    ).fetchone()
    body = json.loads(row[0])
    body["description"] = "silently redefined"  # digest no longer matches the body
    conn.execute(
        "UPDATE score_configs SET config_json=? WHERE name='exact_match' AND version=1",
        (json.dumps(body),),
    )
    conn.commit()
    conn.close()
    with pytest.raises(ScoreConfigPinError, match="corrupt or tampered"):
        resolve_score_config_pin(
            "exact_match@1", metric="exact_match", value_type=ScoreValueType.BOOLEAN, db_path=db
        )


def test_other_sqlite_error_is_reported(tmp_path: Path) -> None:
    path = tmp_path / "registry.db"
    path.write_bytes(b"this is not a sqlite database at all" * 64)
    with pytest.raises(ScoreConfigPinError, match="cannot read score configs"):
        resolve_score_config_pin(
            "exact_match", metric="exact_match", value_type=ScoreValueType.BOOLEAN, db_path=path
        )


# ── experiment record ────────────────────────────────────────────────────────


def _pinned(digest: str | None, ref: str | None = "exact_match@1", tag: str = "") -> Experiment:
    return finalize_experiment(
        Experiment(
            dataset_ref=_DATASET,
            target=_TARGET,
            runs=[],
            aggregate=[],
            status="running",
            score_config_ref=ref,
            score_config_digest=digest,
            labels={"tag": tag} if tag else {},
        )
    )


def test_bad_score_config_digest_rejected() -> None:
    with pytest.raises(ValueError, match="score_config_digest"):
        _pinned("md5:abc")


def test_unpinned_record_hash_unchanged_by_new_optional_field() -> None:
    record = _pinned(None, ref=None)
    body = record.model_dump(mode="json", exclude_none=True)
    assert "score_config_digest" not in body
    assert record.content_hash == experiment_content_hash(record)


def test_pinned_record_validates_against_schema() -> None:
    document = _pinned(_DIG_A).model_dump(mode="json", exclude_none=True)
    assert list(_schema_validator("experiment.schema.json").iter_errors(document)) == []


# ── runner ───────────────────────────────────────────────────────────────────


def _ds(tmp_path: Path) -> Path:
    path = tmp_path / "items.jsonl"
    rows = [{"item_id": f"i{k}", "input": f"a{k}", "expected": f"a{k}"} for k in range(2)]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return path


def test_run_records_resolved_ref_and_digest(tmp_path: Path, db: Path) -> None:
    experiment = run_experiment(
        load_dataset(_ds(tmp_path)),
        _ECHO,
        target=_TARGET,
        runs_dir=tmp_path / "runs",
        score_config_ref="exact_match",
        score_config_db_path=db,
    )
    expected = resolve_config_ref_readonly("exact_match@2", db_path=db)
    assert experiment.score_config_ref == "exact_match@2"  # resolved, never the alias
    assert experiment.score_config_digest == expected.content_digest
    assert experiment.aggregate[0].value == 1.0


def test_run_unresolvable_ref_fails_before_any_item(tmp_path: Path, db: Path) -> None:
    runs_dir = tmp_path / "runs"
    with pytest.raises(ScoreConfigPinError):
        run_experiment(
            load_dataset(_ds(tmp_path)),
            _ECHO,
            target=_TARGET,
            runs_dir=runs_dir,
            score_config_ref="exact_match@7",
            score_config_db_path=db,
        )
    assert not runs_dir.exists()  # no capsule was captured


def test_run_without_ref_records_no_pin(tmp_path: Path) -> None:
    experiment = run_experiment(
        load_dataset(_ds(tmp_path)), _ECHO, target=_TARGET, runs_dir=tmp_path / "runs"
    )
    assert experiment.score_config_ref is None
    assert experiment.score_config_digest is None


# ── comparability ────────────────────────────────────────────────────────────


def test_same_digest_is_comparable() -> None:
    report = score_config_comparability(_pinned(_DIG_A), _pinned(_DIG_A), metric="exact_match")
    assert report.comparable is True and report.status == "same_digest"
    assert report.baseline_digest == report.candidate_digest == _DIG_A


def test_different_digest_is_explicitly_not_comparable() -> None:
    report = score_config_comparability(
        _pinned(_DIG_A), _pinned(_DIG_B, ref="exact_match@2"), metric="exact_match"
    )
    assert report.comparable is False and report.status == "different_digest"
    assert report.baseline_digest == _DIG_A and report.candidate_digest == _DIG_B
    assert "NOT directly comparable" in report.message
    assert _DIG_A in report.message and _DIG_B in report.message


@pytest.mark.parametrize(
    ("base", "cand", "missing"),
    [
        ((None, None), (_DIG_A, "exact_match@1"), "baseline"),
        ((_DIG_A, "exact_match@1"), (None, None), "candidate"),
        ((None, None), (None, None), "baseline and candidate"),
        # legacy record: a ref that was never resolved to a digest is not a pin
        ((None, "score-config://default-pass"), (_DIG_A, "exact_match@1"), "baseline"),
        # a digest pinned for a different metric does not pin this one
        ((_DIG_A, "latency@1"), (_DIG_A, "exact_match@1"), "baseline"),
    ],
)
def test_missing_pin_is_null_never_assumed(
    base: tuple[str | None, str | None], cand: tuple[str | None, str | None], missing: str
) -> None:
    report = score_config_comparability(
        _pinned(base[0], ref=base[1]), _pinned(cand[0], ref=cand[1]), metric="exact_match"
    )
    assert report.comparable is None and report.status == "not_pinned"
    assert f"on the {missing} experiment" in report.message


def test_digest_with_unparseable_ref_still_pins() -> None:
    report = score_config_comparability(
        _pinned(_DIG_A, ref=None), _pinned(_DIG_A, ref="exact_match@1"), metric="exact_match"
    )
    assert report.comparable is True


def _scored(tmp_path: Path, tag: str, digest: str | None) -> Experiment:
    capsule = tmp_path / tag / "i0"
    capsule.mkdir(parents=True)
    score = Score(
        subject=_DIGEST,
        name="exact_match",
        value=True,
        value_type=ScoreValueType.BOOLEAN,
        source=ScoreSource.CODE,
        evaluator_id="ev",
        eval_card_digest=_DIGEST,
    )
    append_score(capsule / SCORES_FILENAME, score)
    return finalize_experiment(
        Experiment(
            dataset_ref=_DATASET,
            target=_TARGET,
            runs=[ItemRun(item_id="i0", capsule_ref=str(capsule), score_ids=[score.score_id])],
            aggregate=[],
            status="running",
            score_config_ref="exact_match@1" if digest else None,
            score_config_digest=digest,
        )
    )


@pytest.mark.parametrize(
    ("base", "cand", "comparable"),
    [(_DIG_A, _DIG_A, True), (_DIG_A, _DIG_B, False), (None, _DIG_B, None)],
)
def test_compare_embeds_block_and_matches_schema(
    tmp_path: Path, base: str | None, cand: str | None, comparable: bool | None
) -> None:
    comparison = compare_experiments(
        _scored(tmp_path, "b", base), _scored(tmp_path, "c", cand), metric="exact_match"
    )
    assert comparison.score_config is not None
    assert comparison.score_config.comparable is comparable
    assert comparison.exit_code == 0  # reported, not gated: ADR-0080 contract unchanged
    document = json.loads(comparison.model_dump_json())
    errors = list(_schema_validator("experiment-comparison.schema.json").iter_errors(document))
    assert errors == [], [e.message for e in errors]
    report = comparison.to_policy_regression_report()
    assert report["score_config"]["comparable"] is comparable
