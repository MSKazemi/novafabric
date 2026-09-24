"""ADR-0236 D3/D4 — `ratio()` as a derived aggregate, and what it does with nothing.

D4 is where a ratio primitive usually goes wrong, and the ADR says so outright:

    "0 errors out of 0 runs" is not a 0% error rate; it is no information, and
    charting it as 0% invents a reassuring data point that never existed.

So every test below that looks like an edge case is the feature.
"""

from __future__ import annotations

import pytest

from novafabric.query import QueryParseError, validate_query_object
from novafabric.query.executor import _derived_value
from novafabric.query.model import DERIVED_FUNCS
from novafabric.query.parser import parse_select


def _ratio_agg(select: str = "count(), sum(cost), ratio(sum(cost), count()) AS cpr"):  # type: ignore[no-untyped-def]
    return next(a for a in parse_select(select) if a.is_derived)


# ---------------------------------------------------------------------------
# D3 — parsing and operand resolution
# ---------------------------------------------------------------------------


def test_ratio_parses_with_operands_resolved() -> None:
    agg = _ratio_agg()
    assert agg.func == "ratio"
    assert agg.alias == "cpr"
    assert (agg.numerator, agg.denominator) == ("sum(cost)", "count()")
    assert agg.metric is None, "a derived function takes no metric"


def test_a_ratio_gets_a_canonical_default_alias() -> None:
    agg = next(a for a in parse_select("count(), ratio(count(), count())") if a.is_derived)
    assert agg.alias == "ratio(count(), count())"
    assert agg.expression == "ratio(count(), count())"


def test_the_select_list_splits_on_top_level_commas_only() -> None:
    """A naive `split(",")` turns `ratio(a, b)` into two malformed halves."""
    aggs = parse_select("count(), sum(cost), ratio(sum(cost), count()) AS cpr")
    assert [a.alias for a in aggs] == ["count()", "sum(cost)", "cpr"]


def test_existing_selects_are_unaffected_by_the_new_split() -> None:
    """Additive: no expression without a top-level comma can change meaning."""
    aggs = parse_select("count(), avg(latency) AS p, p95(cost)")
    assert [a.alias for a in aggs] == ["count()", "p", "p95(cost)"]
    assert not any(a.is_derived for a in aggs)


@pytest.mark.parametrize("operand", ["nope", "a", "sum(tokens)"])
def test_an_operand_that_is_not_selected_is_refused(operand: str) -> None:
    """An operand reference is only meaningful against the whole select list."""
    with pytest.raises(QueryParseError, match="is not selected in this query"):
        parse_select(f"count(), ratio({operand}, count())")


def test_the_refusal_names_what_was_selected() -> None:
    """So the fix is visible without re-reading the DSL reference."""
    with pytest.raises(QueryParseError, match="selected: count\\(\\)"):
        parse_select("count(), ratio(nope, count())")


def test_a_ratio_cannot_take_another_ratio_as_an_operand() -> None:
    """One evaluation pass, base aggregates first — no topological sort."""
    with pytest.raises(QueryParseError, match="cannot be an operand"):
        parse_select(
            "count(), ratio(count(), count()) AS r1, ratio(r1, count()) AS r2"
        )


def test_an_unknown_derived_function_is_refused() -> None:
    """The allow-list is closed; that is ADR-0129's security property."""
    with pytest.raises(QueryParseError, match="unknown"):
        parse_select("count(), quotient(count(), count())")


def test_the_derived_allow_list_is_exactly_ratio() -> None:
    assert DERIVED_FUNCS == ("ratio",)


def test_the_query_object_path_accepts_a_ratio() -> None:
    """`validate_query_object` is what ADR-0235 widgets are checked against."""
    validate_query_object(
        {"select": ["count()", "sum(cost)", "ratio(sum(cost), count()) AS cpr"]}
    )


def test_a_widget_carrying_a_bad_ratio_is_still_refused() -> None:
    """ADR-0235 D7 inherits this check rather than duplicating it."""
    from novafabric.dashboards import WidgetValidationError, load_widget

    with pytest.raises(WidgetValidationError, match="DSL rejects"):
        load_widget(
            {
                "$novafabricWidget": True,
                "version": 1,
                "id": "bad-ratio",
                "title": "Bad",
                "query": {"select": ["ratio(missing, count())"]},
                "presentation": {"chart": "line"},
            }
        )


# ---------------------------------------------------------------------------
# D4 — undefined is not zero
# ---------------------------------------------------------------------------


def test_a_normal_ratio_divides() -> None:
    """Non-vacuity: a function that always returns None would pass every test below."""
    assert _derived_value(_ratio_agg(), {"sum(cost)": 10.0, "count()": 4}) == 2.5


def test_a_zero_denominator_is_undefined_not_zero() -> None:
    """The load-bearing case. 0 out of 0 is no information, not 0%."""
    assert _derived_value(_ratio_agg(), {"sum(cost)": 0.0, "count()": 0}) is None


def test_a_nonzero_numerator_over_zero_is_also_undefined() -> None:
    assert _derived_value(_ratio_agg(), {"sum(cost)": 7.0, "count()": 0}) is None


def test_an_absent_numerator_yields_an_absent_ratio() -> None:
    """A group with no cost rows has no cost, not zero cost."""
    assert _derived_value(_ratio_agg(), {"sum(cost)": None, "count()": 4}) is None


def test_an_absent_denominator_yields_an_absent_ratio() -> None:
    assert _derived_value(_ratio_agg(), {"sum(cost)": 10.0, "count()": None}) is None


def test_a_missing_operand_key_yields_an_absent_ratio() -> None:
    """Never a KeyError on the read path, and never a fabricated zero."""
    assert _derived_value(_ratio_agg(), {}) is None


def test_a_non_numeric_operand_yields_an_absent_ratio() -> None:
    assert _derived_value(_ratio_agg(), {"sum(cost)": "n/a", "count()": 4}) is None


def test_zero_over_nonzero_is_a_real_zero() -> None:
    """The converse. A measured zero must still be reported as zero.

    "absent is not zero" must not become "zero is suspicious" — 0 cost across 4
    priced runs is a fact, and refusing it would make the metric useless.
    """
    assert _derived_value(_ratio_agg(), {"sum(cost)": 0.0, "count()": 4}) == 0.0


# ---------------------------------------------------------------------------
# D7 — the cache version is deliberately NOT bumped
# ---------------------------------------------------------------------------


def test_a_derived_metric_needs_nothing_from_the_indexer() -> None:
    """ADR-0236 D7 asks for a cache bump on a premise that does not hold here.

    `INDEXER_SCHEMA_VERSION` is documented as bumped *"whenever the **indexer**
    changes what it extracts"*. A ratio is arithmetic over aggregates the indexer
    already produced, so bumping for it would discard every user's query cache
    for no reason.

    ⚠ This originally asserted `INDEXER_SCHEMA_VERSION == 1`, which was the wrong
    way to say it: **pinning an absolute version to express "*this* feature did
    not bump it" breaks the moment a different feature legitimately does** — and
    it did, when ADR-0233 added `parent_run_id` and correctly bumped to 2. The
    property being claimed is about *ratio*, so test that: a derived aggregate
    carries no metric, and therefore contributes nothing to what must be
    extracted from a capsule.
    """
    from dataclasses import fields

    from novafabric.query.indexer import CallRow

    agg = _ratio_agg()
    assert agg.is_derived
    assert agg.metric is None, (
        "a derived function that carried a metric would be asking the indexer for "
        "a value, and would then genuinely need a cache bump"
    )
    assert agg.numerator and agg.denominator
    indexed = {f.name for f in fields(CallRow)}
    for operand in (agg.numerator, agg.denominator):
        assert operand not in indexed, (
            f"{operand!r} names an indexed field rather than another select item; "
            "a ratio must compose selected aggregates, not read new capsule data"
        )
