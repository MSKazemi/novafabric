"""Indexer tests — capsule-dir scan into derived index rows (ADR-0129 P2)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import pytest

from novafabric.query import QueryIndexError
from novafabric.query.indexer import scan_capsule_dir

CapsuleFactory = Callable[..., Path]
RecordFactory = Callable[..., dict[str, Any]]


def test_missing_dir_is_clear_error(tmp_path: Path) -> None:
    with pytest.raises(QueryIndexError, match="not found"):
        scan_capsule_dir(tmp_path / "nope")


def test_empty_dir_yields_no_rows(capsule_dir: Path) -> None:
    rows = scan_capsule_dir(capsule_dir)
    assert rows.capsule_count == 0
    assert rows.calls == []
    assert rows.scores == []


def test_non_capsule_entries_ignored(capsule_dir: Path) -> None:
    (capsule_dir / "not-a-capsule").mkdir()
    (capsule_dir / "loose-file.txt").write_text("x")
    rows = scan_capsule_dir(capsule_dir)
    assert rows.capsule_count == 0


def test_model_call_extraction(
    make_capsule: CapsuleFactory,
    capsule_dir: Path,
    model_call: RecordFactory,
    score: RecordFactory,
) -> None:
    make_capsule(
        "run-a",
        metadata={"asset": "summarizer", "tag": "nightly"},
        manifest_extra={"deployment_environment": "production", "variant": "treatment"},
        model_calls=[
            model_call(model="m1", cost=0.02, prompt_tokens=10, completion_tokens=5,
                       duration_ms=100, log_level="warn"),
        ],
        scores=[score("faithfulness", 0.9)],
    )
    rows = scan_capsule_dir(capsule_dir)
    assert rows.capsule_count == 1
    (call,) = rows.calls
    assert call.run_id == "run-a"
    assert call.status == "success"
    assert call.asset == "summarizer"
    assert call.tag == "nightly"
    assert call.deployment_environment == "production"
    assert call.variant == "treatment"
    assert call.model == "m1"
    assert call.model_id == "m1"
    assert call.cost == 0.02
    assert call.prompt_tokens == 10
    assert call.completion_tokens == 5
    assert call.total_tokens == 15
    assert call.latency == 100
    assert call.log_level == "warn"
    (score_row,) = rows.scores
    assert score_row.name == "faithfulness"
    assert score_row.value == 0.9
    assert score_row.asset == "summarizer"
    assert score_row.model is None


def test_zero_call_capsule_gets_synthetic_row(
    make_capsule: CapsuleFactory, capsule_dir: Path
) -> None:
    make_capsule("run-empty", status="failure", model_calls=[])
    rows = scan_capsule_dir(capsule_dir)
    (call,) = rows.calls
    assert call.run_id == "run-empty"
    assert call.status == "failure"
    assert call.model is None
    assert call.cost is None
    assert call.log_level == "info"  # ADR-0127 absent ⇒ info


def test_log_level_defaults_to_info(
    make_capsule: CapsuleFactory, capsule_dir: Path, model_call: RecordFactory
) -> None:
    make_capsule("run-b", model_calls=[model_call()])
    rows = scan_capsule_dir(capsule_dir)
    assert rows.calls[0].log_level == "info"


def test_json_manifest_supported(
    make_capsule: CapsuleFactory, capsule_dir: Path, model_call: RecordFactory
) -> None:
    make_capsule("run-json", manifest_format="json", model_calls=[model_call()])
    rows = scan_capsule_dir(capsule_dir)
    assert rows.capsule_count == 1
    assert rows.calls[0].run_id == "run-json"


def test_variant_block_form(make_capsule: CapsuleFactory, capsule_dir: Path) -> None:
    make_capsule(
        "run-v",
        manifest_extra={"variant": {"experiment": "exp-1", "name": "control"}},
        model_calls=[],
    )
    rows = scan_capsule_dir(capsule_dir)
    assert rows.calls[0].variant == "control"


def test_variant_adr0116_canonical_block(
    make_capsule: CapsuleFactory, capsule_dir: Path
) -> None:
    """The ADR-0116 block's variant_id is the queryable dimension."""
    make_capsule(
        "run-v116",
        manifest_extra={
            "variant": {
                "experiment_id": "exp_2026Q3_system_prompt",
                "variant_id": "concise-v3",
                "variant_label": "concise-system-prompt",
                "assignment_source": "launchdarkly",
            }
        },
        model_calls=[],
    )
    rows = scan_capsule_dir(capsule_dir)
    assert rows.calls[0].variant == "concise-v3"


def test_variant_absent_stays_none(make_capsule: CapsuleFactory, capsule_dir: Path) -> None:
    """Absence changes nothing: no block, no metadata label ⇒ None, never synthesized."""
    make_capsule("run-nv", model_calls=[])
    rows = scan_capsule_dir(capsule_dir)
    assert rows.calls[0].variant is None


def test_boolean_score_becomes_binary(
    make_capsule: CapsuleFactory, capsule_dir: Path, score: RecordFactory
) -> None:
    make_capsule(
        "run-s",
        scores=[
            score("passed", True, value_type="boolean"),
            score("label", "good", value_type="categorical"),  # skipped
        ],
    )
    rows = scan_capsule_dir(capsule_dir)
    (score_row,) = rows.scores  # categorical skipped
    assert score_row.name == "passed"
    assert score_row.value == 1.0


def test_missing_cost_is_null_not_zero(
    make_capsule: CapsuleFactory, capsule_dir: Path, model_call: RecordFactory
) -> None:
    make_capsule("run-nc", model_calls=[model_call(cost=None)])
    rows = scan_capsule_dir(capsule_dir)
    assert rows.calls[0].cost is None


def test_corrupt_manifest_fails_closed(capsule_dir: Path) -> None:
    bad = capsule_dir / "run-bad"
    bad.mkdir()
    (bad / "capsule.json").write_text("{not json")
    with pytest.raises(QueryIndexError, match="unreadable"):
        scan_capsule_dir(capsule_dir)


def test_manifest_missing_created_at_fails_closed(capsule_dir: Path) -> None:
    bad = capsule_dir / "run-bad"
    bad.mkdir()
    (bad / "capsule.json").write_text(json.dumps({"run_id": "run-bad"}))
    with pytest.raises(QueryIndexError, match="created_at"):
        scan_capsule_dir(capsule_dir)


def test_corrupt_model_call_line_fails_closed(
    make_capsule: CapsuleFactory, capsule_dir: Path
) -> None:
    capsule = make_capsule("run-c")
    (capsule / "model-calls.jsonl").write_text("{broken\n")
    with pytest.raises(QueryIndexError, match="model-call"):
        scan_capsule_dir(capsule_dir)


def test_scan_is_read_only(
    make_capsule: CapsuleFactory,
    capsule_dir: Path,
    model_call: RecordFactory,
    score: RecordFactory,
) -> None:
    make_capsule("run-ro", model_calls=[model_call()], scores=[score("m", 1.0)])
    before = sorted(p.relative_to(capsule_dir) for p in capsule_dir.rglob("*"))
    mtimes = {p: p.stat().st_mtime_ns for p in capsule_dir.rglob("*") if p.is_file()}
    scan_capsule_dir(capsule_dir)
    after = sorted(p.relative_to(capsule_dir) for p in capsule_dir.rglob("*"))
    assert before == after
    assert mtimes == {p: p.stat().st_mtime_ns for p in capsule_dir.rglob("*") if p.is_file()}
