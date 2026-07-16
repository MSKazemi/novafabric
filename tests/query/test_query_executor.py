"""Execution tests — aggregation correctness over both engines (ADR-0129 P3)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import pytest

from novafabric.query import QueryExecutionError, build_plan, run_query
from novafabric.query.executor import _percentile

CapsuleFactory = Callable[..., Path]
RecordFactory = Callable[..., dict[str, Any]]

NOW = datetime(2026, 7, 12, 14, 0, 0, tzinfo=timezone.utc)

pytestmark = pytest.mark.parametrize("engine", ["duckdb", "sqlite"])


def _seed(make_capsule: CapsuleFactory, model_call: RecordFactory, score: RecordFactory) -> None:
    make_capsule(
        "run-1",
        created_at="2026-07-10T00:00:00Z",
        metadata={"asset": "summarizer", "tag": "nightly"},
        manifest_extra={"deployment_environment": "production"},
        model_calls=[
            model_call(model="m1", cost=0.01, duration_ms=100),
            model_call(model="m1", cost=0.03, duration_ms=300),
        ],
        scores=[score("faithfulness", 0.8)],
    )
    make_capsule(
        "run-2",
        created_at="2026-07-11T00:00:00Z",
        metadata={"asset": "summarizer", "tag": "canary"},
        manifest_extra={"deployment_environment": "production"},
        model_calls=[model_call(model="m2", cost=0.10, duration_ms=500)],
        scores=[score("faithfulness", 0.6)],
    )
    make_capsule(
        "run-3",
        created_at="2026-07-01T00:00:00Z",  # outside a 7d window ending at NOW
        status="failure",
        metadata={"asset": "router"},
        manifest_extra={"deployment_environment": "staging"},
        model_calls=[model_call(model="m1", cost=0.50, duration_ms=900)],
    )
    make_capsule("run-4", created_at="2026-07-11T12:00:00Z", model_calls=[])  # zero calls


def test_count_all_capsules(
    engine: str,
    make_capsule: CapsuleFactory,
    capsule_dir: Path,
    model_call: RecordFactory,
    score: RecordFactory,
) -> None:
    _seed(make_capsule, model_call, score)
    plan = build_plan(select="count()")
    result = run_query(plan, capsule_dir, engine=engine, now=NOW)
    assert result["rows"] == [{"count()": 4}]  # distinct capsules, not calls
    assert result["row_count"] == 1
    assert result["truncated"] is False
    assert result["index"]["engine"] == engine
    assert result["index"]["capsule_count"] == 4
    assert result["columns"] == ["count()"]


def test_avg_skips_null_metrics(
    engine: str,
    make_capsule: CapsuleFactory,
    capsule_dir: Path,
    model_call: RecordFactory,
    score: RecordFactory,
) -> None:
    _seed(make_capsule, model_call, score)
    # run-4 has no model call → no cost value; avg over the 4 real costs.
    plan = build_plan(select="avg(cost) AS avg_cost, sum(cost) AS total")
    result = run_query(plan, capsule_dir, engine=engine, now=NOW)
    (row,) = result["rows"]
    assert row["total"] == pytest.approx(0.64)
    assert row["avg_cost"] == pytest.approx(0.64 / 4)


def test_group_by_model(
    engine: str,
    make_capsule: CapsuleFactory,
    capsule_dir: Path,
    model_call: RecordFactory,
    score: RecordFactory,
) -> None:
    _seed(make_capsule, model_call, score)
    plan = build_plan(
        select="avg(cost) AS avg_cost, count() AS runs",
        group_by=["model"],
        order_by={"by": "avg_cost", "direction": "desc"},
    )
    result = run_query(plan, capsule_dir, engine=engine, now=NOW)
    assert result["columns"] == ["model", "avg_cost", "runs"]
    rows = result["rows"]
    by_model = {row["model"]: row for row in rows}
    assert by_model["m1"]["runs"] == 2  # run-1 and run-3 (distinct capsules)
    assert by_model["m1"]["avg_cost"] == pytest.approx((0.01 + 0.03 + 0.50) / 3)
    assert by_model["m2"]["runs"] == 1
    assert by_model[None]["runs"] == 1  # zero-call capsule groups under null
    # desc ordering with None (null group) last
    assert [row["model"] for row in rows] == ["m1", "m2", None]


def test_where_filters(
    engine: str,
    make_capsule: CapsuleFactory,
    capsule_dir: Path,
    model_call: RecordFactory,
    score: RecordFactory,
) -> None:
    _seed(make_capsule, model_call, score)
    plan = build_plan(
        select="count()",
        where="deployment_environment = production AND status = success",
    )
    result = run_query(plan, capsule_dir, engine=engine, now=NOW)
    assert result["rows"] == [{"count()": 2}]

    plan = build_plan(select="count()", where="status != success")
    result = run_query(plan, capsule_dir, engine=engine, now=NOW)
    assert result["rows"] == [{"count()": 1}]

    plan = build_plan(select="count()", where="tag IN (nightly, canary)")
    result = run_query(plan, capsule_dir, engine=engine, now=NOW)
    assert result["rows"] == [{"count()": 2}]


def test_variant_filter_over_adr0116_blocks(
    engine: str,
    make_capsule: CapsuleFactory,
    capsule_dir: Path,
    model_call: RecordFactory,
) -> None:
    """ADR-0116 read convenience: filter/group by the recorded variant_id."""
    for run_id, arm in (("run-a", "control"), ("run-b", "treatment"), ("run-c", "treatment")):
        make_capsule(
            run_id,
            created_at="2026-07-11T00:00:00Z",
            manifest_extra={
                "variant": {
                    "experiment_id": "exp-1",
                    "variant_id": arm,
                    "assignment_source": "statsig",
                }
            },
            model_calls=[model_call(model="m1", cost=0.01)],
        )
    make_capsule("run-nv", created_at="2026-07-11T00:00:00Z", model_calls=[])  # no variant

    plan = build_plan(select="count()", where="variant = treatment")
    result = run_query(plan, capsule_dir, engine=engine, now=NOW)
    assert result["rows"] == [{"count()": 2}]

    plan = build_plan(select="count()", group_by="variant")
    result = run_query(plan, capsule_dir, engine=engine, now=NOW)
    by_variant = {row["variant"]: row["count()"] for row in result["rows"]}
    assert by_variant == {"control": 1, "treatment": 2, None: 1}


def test_absent_dimension_never_matches(
    engine: str,
    make_capsule: CapsuleFactory,
    capsule_dir: Path,
    model_call: RecordFactory,
    score: RecordFactory,
) -> None:
    _seed(make_capsule, model_call, score)
    # run-4 has no deployment_environment: != excludes NULL (SQL semantics).
    plan = build_plan(select="count()", where="deployment_environment != production")
    result = run_query(plan, capsule_dir, engine=engine, now=NOW)
    assert result["rows"] == [{"count()": 1}]  # only run-3 (staging)


def test_log_level_severity_ordering(
    engine: str, make_capsule: CapsuleFactory, capsule_dir: Path, model_call: RecordFactory
) -> None:
    make_capsule(
        "run-lv",
        created_at="2026-07-10T00:00:00Z",
        model_calls=[
            model_call(model="m1", log_level="debug"),
            model_call(model="m2", log_level="warn"),
            model_call(model="m3", log_level="error"),
        ],
    )
    plan = build_plan(select="count()", where="log_level >= warning", group_by=["model"])
    result = run_query(plan, capsule_dir, engine=engine, now=NOW)
    assert sorted(row["model"] for row in result["rows"]) == ["m2", "m3"]


def test_time_window(
    engine: str,
    make_capsule: CapsuleFactory,
    capsule_dir: Path,
    model_call: RecordFactory,
    score: RecordFactory,
) -> None:
    _seed(make_capsule, model_call, score)
    plan = build_plan(select="count()", since="7d")
    result = run_query(plan, capsule_dir, engine=engine, now=NOW)
    assert result["rows"] == [{"count()": 3}]  # run-3 (07-01) is outside
    assert result["time_window"]["since"] == "2026-07-05T14:00:00Z"
    assert result["time_window"]["until"] == "2026-07-12T14:00:00Z"

    plan = build_plan(
        select="count()",
        since="2026-07-10T00:00:00Z",
        until="2026-07-11T00:00:00Z",  # exclusive: run-2 (at 00:00) excluded
    )
    result = run_query(plan, capsule_dir, engine=engine, now=NOW)
    assert result["rows"] == [{"count()": 1}]


def test_percentiles_linear_interpolation(
    engine: str, make_capsule: CapsuleFactory, capsule_dir: Path, model_call: RecordFactory
) -> None:
    make_capsule(
        "run-p",
        created_at="2026-07-10T00:00:00Z",
        model_calls=[model_call(duration_ms=d) for d in (100, 200, 300, 400)],
    )
    plan = build_plan(select="p50(latency) AS p50, p95(latency) AS p95, max(latency) AS mx")
    result = run_query(plan, capsule_dir, engine=engine, now=NOW)
    (row,) = result["rows"]
    assert row["p50"] == pytest.approx(250.0)
    assert row["p95"] == pytest.approx(385.0)  # 300 + 0.85 * 100
    assert row["mx"] == 400.0


def test_score_aggregation(
    engine: str,
    make_capsule: CapsuleFactory,
    capsule_dir: Path,
    model_call: RecordFactory,
    score: RecordFactory,
) -> None:
    _seed(make_capsule, model_call, score)
    plan = build_plan(
        select="avg(score[faithfulness]) AS avg_f, count()",
        where="asset = summarizer",
    )
    result = run_query(plan, capsule_dir, engine=engine, now=NOW)
    (row,) = result["rows"]
    assert row["avg_f"] == pytest.approx(0.7)
    assert row["count()"] == 2


def test_score_missing_name_yields_null(
    engine: str,
    make_capsule: CapsuleFactory,
    capsule_dir: Path,
    model_call: RecordFactory,
    score: RecordFactory,
) -> None:
    _seed(make_capsule, model_call, score)
    plan = build_plan(select="avg(score[nonexistent]) AS avg_x, count()")
    result = run_query(plan, capsule_dir, engine=engine, now=NOW)
    (row,) = result["rows"]
    assert row["avg_x"] is None
    assert row["count()"] == 4


def test_empty_dir_returns_no_rows(engine: str, capsule_dir: Path) -> None:
    plan = build_plan(select="count()")
    result = run_query(plan, capsule_dir, engine=engine, now=NOW)
    assert result["rows"] == []
    assert result["row_count"] == 0
    assert result["truncated"] is False
    assert result["index"]["capsule_count"] == 0


def test_no_match_returns_no_rows(
    engine: str,
    make_capsule: CapsuleFactory,
    capsule_dir: Path,
    model_call: RecordFactory,
    score: RecordFactory,
) -> None:
    _seed(make_capsule, model_call, score)
    plan = build_plan(select="count()", where="asset = nonexistent")
    result = run_query(plan, capsule_dir, engine=engine, now=NOW)
    assert result["rows"] == []
    assert result["row_count"] == 0


def test_limit_truncates_after_ordering(
    engine: str,
    make_capsule: CapsuleFactory,
    capsule_dir: Path,
    model_call: RecordFactory,
    score: RecordFactory,
) -> None:
    _seed(make_capsule, model_call, score)
    plan = build_plan(
        select="sum(cost) AS total",
        group_by=["model"],
        limit=1,
        order_by={"by": "total", "direction": "desc"},
    )
    result = run_query(plan, capsule_dir, engine=engine, now=NOW)
    assert result["truncated"] is True
    assert result["row_count"] == 1
    assert result["rows"][0]["model"] == "m1"  # top-N stable: ordering before truncation


def test_group_cardinality_cap(
    engine: str,
    make_capsule: CapsuleFactory,
    capsule_dir: Path,
    model_call: RecordFactory,
    score: RecordFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed(make_capsule, model_call, score)
    import novafabric.query.executor as executor_mod

    monkeypatch.setattr(executor_mod, "MAX_GROUPS", 2)
    plan = build_plan(select="count()", group_by=["model"])
    with pytest.raises(QueryExecutionError, match="narrow the group_by"):
        run_query(plan, capsule_dir, engine=engine, now=NOW)


def test_engines_agree(
    engine: str,
    make_capsule: CapsuleFactory,
    capsule_dir: Path,
    model_call: RecordFactory,
    score: RecordFactory,
) -> None:
    _seed(make_capsule, model_call, score)
    plan = build_plan(
        select="avg(cost) AS avg_cost, p95(latency) AS p95, count()",
        group_by=["model", "status"],
    )
    result = run_query(plan, capsule_dir, engine=engine, now=NOW)
    reference = run_query(plan, capsule_dir, engine="duckdb", now=NOW)
    assert result["rows"] == reference["rows"]


def test_result_shape_matches_spec(
    engine: str,
    make_capsule: CapsuleFactory,
    capsule_dir: Path,
    model_call: RecordFactory,
    score: RecordFactory,
) -> None:
    _seed(make_capsule, model_call, score)
    plan = build_plan(select="avg(cost) AS avg_cost, count() AS runs", group_by=["model"])
    result = run_query(plan, capsule_dir, engine=engine, now=NOW)
    assert set(result) == {
        "schema_version",
        "generated_at",
        "query",
        "time_window",
        "columns",
        "rows",
        "row_count",
        "truncated",
        "index",
    }
    assert result["schema_version"] == "0.1.0"
    assert set(result["index"]) == {"engine", "built_at", "capsule_count"}
    assert set(result["time_window"]) == {"since", "until"}
    assert result["query"]["select"] == ["avg(cost) AS avg_cost", "count() AS runs"]
    for row in result["rows"]:
        assert list(row) == result["columns"]


@pytest.mark.parametrize(
    "values,p,expected",
    [
        ([10.0], 95, 10.0),
        ([1.0, 2.0], 50, 1.5),
        ([1.0, 2.0, 3.0], 0, 1.0),
        ([1.0, 2.0, 3.0], 99, 2.98),
    ],
)
def test_percentile_helper(engine: str, values: list[float], p: int, expected: float) -> None:
    assert _percentile(values, p) == pytest.approx(expected)
