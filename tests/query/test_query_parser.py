"""Parser tests for the Capsule Query DSL v0 (ADR-0129 P1).

The invalid cases mirror the golden fixtures in
``design/spec/fixtures/capsule-query-dsl/`` (replicated inline — the design
directory is private and not part of the public mirror).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from novafabric.query import QueryParseError, build_plan, load_query_file, plan_from_query_object
from novafabric.query.parser import parse_predicate, parse_select_item

# ---------------------------------------------------------------------------
# select — allow-list
# ---------------------------------------------------------------------------


def test_count_parses() -> None:
    agg = parse_select_item("count()")
    assert agg.func == "count"
    assert agg.metric is None
    assert agg.alias == "count()"


def test_aggregate_with_alias() -> None:
    agg = parse_select_item("avg(cost) AS avg_cost")
    assert (agg.func, agg.metric, agg.alias) == ("avg", "cost", "avg_cost")
    assert agg.normalized() == "avg(cost) AS avg_cost"


def test_percentile_parses() -> None:
    agg = parse_select_item("p95(latency)")
    assert agg.func == "p95"
    assert agg.metric == "latency"
    assert agg.alias == "p95(latency)"


def test_score_with_name_parses() -> None:
    agg = parse_select_item("avg(score[faithfulness])")
    assert agg.metric == "score"
    assert agg.score_name == "faithfulness"
    assert agg.expression == "avg(score[faithfulness])"


@pytest.mark.parametrize(
    "expr,needle",
    [
        ("median(cost)", "median"),  # fixture: query-invalid-bad-function
        ("avg(temperature)", "temperature"),  # fixture: query-invalid-bad-metric
        ("avg(score)", "score"),  # fixture: query-invalid-score-without-name
        ("p100(latency)", "p100"),  # percentile must be 0..99
        ("count(cost)", "count"),  # count takes no metric
        ("sum()", "sum"),  # sum requires a metric
        ("avg(cost); DROP TABLE calls", "avg"),  # no SQL passthrough
    ],
)
def test_invalid_select_rejected(expr: str, needle: str) -> None:
    with pytest.raises(QueryParseError) as exc_info:
        parse_select_item(expr)
    assert needle in str(exc_info.value)


def test_duplicate_alias_rejected() -> None:
    with pytest.raises(QueryParseError, match="duplicate"):
        build_plan(select="avg(cost) AS x, sum(cost) AS x")


# ---------------------------------------------------------------------------
# where — allow-list
# ---------------------------------------------------------------------------


def test_predicate_parses_with_and_without_spaces() -> None:
    for text in ("asset = summarizer", "asset=summarizer"):
        pred = parse_predicate(text)
        assert (pred.dimension, pred.op, pred.value) == ("asset", "=", "summarizer")
        assert pred.normalized() == "asset = summarizer"


def test_in_predicate_parses() -> None:
    pred = parse_predicate("tag IN (nightly, canary)")  # fixture: query-valid-in-operator
    assert pred.op == "IN"
    assert pred.values == ("nightly", "canary")


def test_quoted_values_unquoted() -> None:
    pred = parse_predicate("model = 'gpt-4o-mini'")
    assert pred.value == "gpt-4o-mini"


def test_log_level_warning_alias_normalized() -> None:
    pred = parse_predicate("log_level >= warning")  # fixture: query-valid-percentile-score
    assert pred.value == "warn"


@pytest.mark.parametrize(
    "text",
    [
        "asset ~ summ",  # fixture: query-invalid-bad-operator
        "user = bob",  # fixture: query-invalid-bad-where-field
        "asset =",  # empty value
        "log_level = loud",  # closed log_level enum
        "tag IN ()",  # empty IN list
        "lower(asset) = x",  # no function calls
    ],
)
def test_invalid_predicate_rejected(text: str) -> None:
    with pytest.raises(QueryParseError):
        parse_predicate(text)


def test_where_and_split() -> None:
    plan = build_plan(
        select="count()",
        where="asset = summarizer AND deployment_environment = production",
    )
    assert [p.dimension for p in plan.where] == ["asset", "deployment_environment"]


# ---------------------------------------------------------------------------
# group_by / limit / order_by / time window
# ---------------------------------------------------------------------------


def test_group_by_metric_rejected() -> None:
    # fixture: query-invalid-group-by-metric
    with pytest.raises(QueryParseError, match="cost"):
        build_plan(select="count()", group_by=["cost"])


def test_group_by_unknown_dimension_rejected() -> None:
    with pytest.raises(QueryParseError, match="user"):
        build_plan(select="count()", group_by=["user"])


def test_limit_ceiling_enforced() -> None:
    # fixture: query-invalid-limit-too-high
    with pytest.raises(QueryParseError, match="10000"):
        build_plan(select="count()", limit=10001)
    with pytest.raises(QueryParseError):
        build_plan(select="count()", limit=0)


def test_limit_default() -> None:
    assert build_plan(select="count()").limit == 100


def test_order_by_defaults_to_first_select_desc() -> None:
    plan = build_plan(select="avg(cost) AS avg_cost, count()")
    assert plan.order_by.by == "avg_cost"
    assert plan.order_by.direction == "desc"


def test_order_by_object_form() -> None:
    # fixture: query-valid-order-by
    plan = build_plan(
        select="avg(cost) AS avg_cost, count()",
        group_by=["model"],
        order_by={"by": "avg_cost", "direction": "asc"},
    )
    assert plan.order_by.direction == "asc"


def test_order_by_unknown_alias_rejected() -> None:
    with pytest.raises(QueryParseError, match="not a selected"):
        build_plan(select="count()", order_by="nope")


@pytest.mark.parametrize("since", ["7d", "24h", "30m", "45s", "P30D", "PT6H", "2026-07-01T00:00:00Z"])
def test_valid_since_forms(since: str) -> None:
    assert build_plan(select="count()", since=since).since == since


@pytest.mark.parametrize("since", ["yesterday", "7 days", "P", "-3d"])
def test_invalid_since_rejected(since: str) -> None:
    with pytest.raises(QueryParseError):
        build_plan(select="count()", since=since)


def test_invalid_until_rejected() -> None:
    with pytest.raises(QueryParseError, match="until"):
        build_plan(select="count()", until="7d")


# ---------------------------------------------------------------------------
# query object file
# ---------------------------------------------------------------------------


def test_query_file_json_roundtrip(tmp_path: Path) -> None:
    # fixture: query-valid-flag-equiv
    obj = {
        "schema_version": "0.1.0",
        "select": ["avg(cost) AS avg_cost", "count() AS runs"],
        "where": ["asset = summarizer", "deployment_environment = production"],
        "group_by": ["model"],
        "since": "7d",
        "limit": 100,
    }
    path = tmp_path / "q.json"
    path.write_text(json.dumps(obj))
    plan = plan_from_query_object(load_query_file(path))
    assert plan.to_query_object() == {**obj, "order_by": {"by": "avg_cost", "direction": "desc"}}


def test_query_file_yaml(tmp_path: Path) -> None:
    path = tmp_path / "q.yaml"
    path.write_text("select:\n  - count()\nwhere: tag IN (nightly, canary)\n")
    plan = plan_from_query_object(load_query_file(path))
    assert plan.where[0].values == ("nightly", "canary")


def test_query_file_unknown_clause_rejected(tmp_path: Path) -> None:
    # fixture: query-invalid-unknown-key
    path = tmp_path / "q.json"
    path.write_text(json.dumps({"select": ["count()"], "having": "x"}))
    with pytest.raises(QueryParseError, match="having"):
        load_query_file(path)


def test_query_file_bad_schema_version_rejected(tmp_path: Path) -> None:
    path = tmp_path / "q.json"
    path.write_text(json.dumps({"schema_version": "9.9.9", "select": ["count()"]}))
    with pytest.raises(QueryParseError, match="schema_version"):
        load_query_file(path)


def test_query_file_not_an_object_rejected(tmp_path: Path) -> None:
    path = tmp_path / "q.json"
    path.write_text(json.dumps(["count()"]))
    with pytest.raises(QueryParseError, match="single query object"):
        load_query_file(path)


def test_query_file_missing_rejected(tmp_path: Path) -> None:
    with pytest.raises(QueryParseError, match="cannot read"):
        load_query_file(tmp_path / "missing.yaml")


def test_no_select_rejected() -> None:
    # fixture: query-invalid-no-select
    with pytest.raises(QueryParseError, match="select"):
        plan_from_query_object({"where": ["asset = summarizer"], "group_by": ["model"]})


def test_flags_override_file_fields(tmp_path: Path) -> None:
    obj = {"select": ["count()"], "since": "30d", "limit": 5}
    plan = plan_from_query_object(obj, since="7d", select="sum(cost)")
    assert plan.since == "7d"
    assert plan.selects[0].expression == "sum(cost)"
    assert plan.limit == 5  # not overridden — file value kept


@pytest.mark.parametrize(
    "field,value",
    [
        ("select", 42),
        ("where", {"asset": "x"}),
        ("group_by", "model=bad"),  # string form still validated per dimension
        ("limit", "many"),
        ("order_by", 3),
        ("since", 7),
        ("until", 7),
    ],
)
def test_query_object_field_types_validated(field: str, value: object) -> None:
    obj: dict[str, object] = {"select": ["count()"]}
    obj[field] = value
    with pytest.raises(QueryParseError):
        plan_from_query_object(obj)
