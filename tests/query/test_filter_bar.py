"""ADR-0232 D1/D3 — the filter-bar grammar, and bounded observed-value suggestions.

D1's load-bearing sentence:

    No predicate is expressible in the bar that is not expressible in `nova query`.

That is a **security** property, not an ergonomic one: the DSL's closed allow-list
is what ADR-0235 D7 leans on when validating a widget from an untrusted source, so
a bar that could express more would be a second, unreviewed query surface.

``test_the_bar_never_exceeds_the_dsl`` is the guard for it, and it earned its keep
on the first run — see the note there.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from novafabric.query import QueryParseError, observed_values, parse_filter_bar
from novafabric.query.filterbar import MAX_SUGGESTIONS
from novafabric.query.model import DIMENSIONS, NUMERIC_METRICS
from novafabric.query.parser import parse_where

# ---------------------------------------------------------------------------
# D1 — the grammar, and the ceiling
# ---------------------------------------------------------------------------


def test_an_empty_bar_is_no_predicates() -> None:
    for empty in ("", "   ", "\t"):
        assert parse_filter_bar(empty) == ()


def test_a_plain_term_is_equality() -> None:
    (pred,) = parse_filter_bar("status:error")
    assert (pred.dimension, pred.op, pred.value) == ("status", "=", "error")


def test_a_leading_dash_excludes() -> None:
    (pred,) = parse_filter_bar("-model:gpt-4")
    assert (pred.dimension, pred.op, pred.value) == ("model", "!=", "gpt-4")


def test_terms_are_anded() -> None:
    preds = parse_filter_bar("status:error -model:gpt-4 asset:summarizer")
    assert [p.dimension for p in preds] == ["status", "model", "asset"]


def test_a_quoted_value_may_contain_spaces() -> None:
    (pred,) = parse_filter_bar('asset:"my summarizer"')
    assert pred.value == "my summarizer"


def test_an_unbalanced_quote_is_an_error_not_a_truncation() -> None:
    """Silently closing the quote would filter on a value never finished."""
    with pytest.raises(QueryParseError, match="unbalanced quote"):
        parse_filter_bar('asset:"unterminated')


@pytest.mark.parametrize(
    "term", ["status", "status:", ":error", "-:"]
)
def test_a_malformed_term_is_refused(term: str) -> None:
    with pytest.raises(QueryParseError):
        parse_filter_bar(term)


# ---------------------------------------------------------------------------
# The rule that outranks the ADR's own example
# ---------------------------------------------------------------------------


def test_a_glob_is_refused_and_the_message_explains_the_ceiling() -> None:
    """ADR-0232 D1 illustrates the bar with `-model:gpt-4*`. There is no glob
    operator anywhere in the DSL, so the example contradicts the rule beside it.

    Refusing beats matching literally: a literal `gpt-4*` matches nothing, and the
    user reads "no results" as "no such model".
    """
    for pattern in ("model:gpt-4*", "asset:sum?ary", "status:err%"):
        with pytest.raises(QueryParseError, match="wildcard|pattern"):
            parse_filter_bar(pattern)


def test_a_metric_filter_is_refused() -> None:
    """The other half of the ADR's inexpressible example.

    `parse_predicate` accepts only the eight DIMENSIONS — a numeric metric is
    something the DSL *aggregates*, not something it filters by, and
    `nova query --where 'cost > 0.5'` raises today.
    """
    for metric in NUMERIC_METRICS:
        with pytest.raises(QueryParseError, match="is a metric"):
            parse_filter_bar(f"{metric}:>0.5")


def test_the_adr_example_is_rejected_as_written() -> None:
    """Recorded deliberately: two of its three terms are not expressible.

    If a future change makes it parse, either the DSL gained wildcards and metric
    predicates (fine — update this test), or the bar quietly became more powerful
    than the CLI (not fine — that is the defect this file exists to prevent).
    """
    with pytest.raises(QueryParseError):
        parse_filter_bar("status:error -model:gpt-4* cost:>0.5")


@pytest.mark.parametrize(
    ("bar", "dsl"),
    [
        ("status:error", "status = error"),
        ("-model:gpt-4", "model != gpt-4"),
        ("asset:summarizer", "asset = summarizer"),
        ("log_level:>=warn", "log_level >= warn"),
        ('variant:"v 2"', 'variant = "v 2"'),
    ],
)
def test_the_bar_and_the_dsl_produce_the_same_predicate(bar: str, dsl: str) -> None:
    assert parse_filter_bar(bar) == parse_where([dsl])


def test_the_bar_never_exceeds_the_dsl() -> None:
    """Every predicate the bar accepts must survive the DSL's own parser.

    ⚠ This caught a real defect on its first run. The first implementation
    accepted `cost:>0.5` — the ADR's own example — and produced a `Predicate`
    that `parse_predicate` rejects, because metrics are not filterable. The bar
    had silently become a second query surface with semantics the CLI cannot
    reproduce, which is precisely what D1 forbids.
    """
    # `log_level` is a closed set (ADR-0127), so it gets a legal value; every
    # other dimension is free text. Using "value" for log_level is exactly the
    # case that exposed the original hand-maintained parity defect.
    def _value_for(dim: str) -> str:
        return "warn" if dim == "log_level" else "value"

    candidates = [
        *(f"{dim}:{_value_for(dim)}" for dim in DIMENSIONS),
        *(f"-{dim}:{_value_for(dim)}" for dim in DIMENSIONS),
        "log_level:>=warn",
        "log_level:<error",
        'asset:"two words"',
    ]
    for term in candidates:
        for pred in parse_filter_bar(term):
            round_tripped = parse_where([pred.normalized()])
            assert round_tripped == (pred,), (
                f"{term!r} produced {pred.normalized()!r}, which the DSL parses "
                "differently or not at all — the bar has exceeded its ceiling"
            )


def test_an_unknown_field_names_what_is_allowed() -> None:
    """A filter bar is where people type guesses; an error must teach the vocabulary."""
    with pytest.raises(QueryParseError, match="allowed:"):
        parse_filter_bar("nope:x")


def test_the_bar_is_not_stricter_than_the_dsl_either() -> None:
    """The ceiling is the DSL's — which means matching it, not undercutting it.

    An earlier version refused ordering operators on any dimension except
    `log_level`, on the reasonable-sounding grounds that `status > error` is
    odd. But `nova query --where 'status > error'` **is** accepted, so refusing
    it in the bar would make the "typing convenience" unable to type something
    the CLI supports — breaking D1's premise from the other direction.

    Delegating to `parse_predicate` removed the judgement call entirely: this
    module decides syntax, the DSL decides semantics.
    """
    assert parse_filter_bar("status:>error") == parse_where(["status > error"])


def test_log_level_ordering_works() -> None:
    """`log_level` is severity-ranked (ADR-0127), and the bar inherits that."""
    (pred,) = parse_filter_bar("log_level:>=warn")
    assert pred.op == ">="


def test_double_negation_is_refused() -> None:
    with pytest.raises(QueryParseError, match="negates twice"):
        parse_filter_bar("-status:!=error")


# ---------------------------------------------------------------------------
# D3 — suggestions are bounded, and say so
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Row:
    model: str | None = None
    status: str | None = None


def test_suggestions_are_the_observed_values() -> None:
    """The difference between an autocomplete that helps and one that lies."""
    result = observed_values("model", [_Row(model="a"), _Row(model="b"), _Row(model="a")])
    assert result.values == ("a", "b")
    assert result.truncated is False


def test_absent_values_are_skipped_not_rendered_as_empty() -> None:
    result = observed_values("model", [_Row(model=None), _Row(model="a")])
    assert result.values == ("a",)


def test_exceeding_the_cap_is_reported() -> None:
    """"A silently truncated suggestion list teaches users that a value does not exist.""" ""
    rows = [_Row(model=f"m{i}") for i in range(10)]
    result = observed_values("model", rows, limit=3)
    assert len(result.values) == 3
    assert result.truncated is True
    assert result.as_dict()["truncated"] is True


def test_not_exceeding_the_cap_is_not_reported_as_truncated() -> None:
    """The converse — otherwise `truncated` carries no information."""
    rows = [_Row(model=f"m{i}") for i in range(3)]
    assert observed_values("model", rows, limit=10).truncated is False


def test_the_default_cap_exists_and_is_bounded() -> None:
    assert 0 < MAX_SUGGESTIONS <= 1000


def test_an_unknown_dimension_raises_rather_than_returning_empty() -> None:
    """Empty means "this dimension has no values here" — a different claim.

    Conflating them would let a typo read as evidence that nothing exists.
    """
    with pytest.raises(QueryParseError, match="unknown dimension"):
        observed_values("nope", [])


def test_a_metric_is_not_a_suggestible_dimension() -> None:
    for metric in NUMERIC_METRICS:
        with pytest.raises(QueryParseError, match="unknown dimension"):
            observed_values(metric, [])


def test_a_closed_set_dimension_rejects_an_illegal_value() -> None:
    """The defect delegation removed, pinned as behaviour.

    `log_level` is a closed set. A hand-maintained bar happily produced
    `log_level = value`, which `nova query` refuses — the bar had exceeded its
    ceiling on a dimension nobody thought about. Now the DSL's own parser
    decides, so the class cannot recur for a future closed-set dimension either.
    """
    with pytest.raises(QueryParseError, match="log_level"):
        parse_filter_bar("log_level:definitely-not-a-level")


def test_a_rejection_names_the_term_it_came_from() -> None:
    """With several terms in the bar, "invalid predicate" is not actionable."""
    with pytest.raises(QueryParseError, match="log_level:nope"):
        parse_filter_bar("status:error log_level:nope asset:x")
