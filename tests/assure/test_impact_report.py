"""Model-update impact report — ADR-0147 D3 / NF-154.

Three of these tests exist because the corresponding mistake produces a report that
looks precise and is wrong: a missing cost summed as zero, minor units added across
currencies, and counts that do not sum to `n`. A fourth pins that the report offers
no adoption verdict, which the ADR forbids.
"""

from __future__ import annotations

import json

import pytest

from novafabric.assure.impact import (
    ImpactError,
    ImpactReport,
    RunOutcome,
    attach_facet,
    build_report,
    facet_from_capsule,
)
from novafabric.cost.attribution import Money


def _eur(minor: int) -> Money:
    return Money(amount_minor=minor, currency="EUR")


def _run(bid: str, equivalent: bool | None, distance: float | None = None,
         before: int | None = None, after: int | None = None,
         tb: int | None = None, ta: int | None = None) -> RunOutcome:
    return RunOutcome(
        baseline_id=bid, equivalent=equivalent, distance=distance,
        cost_before=_eur(before) if before is not None else None,
        cost_after=_eur(after) if after is not None else None,
        tokens_before=tb, tokens_after=ta,
    )


CORPUS = [
    _run("bl-1", True, 0.0, 100, 90, 1000, 900),
    _run("bl-2", False, 0.4, 200, 260, 2000, 2400),
    _run("bl-3", False, 0.8, 150, 150, 1500, 1500),
    _run("bl-4", None),
]


@pytest.fixture()
def report() -> ImpactReport:
    return build_report(CORPUS, from_model="gpt-a", to_model="gpt-b")


# ── the MUST fields ──────────────────────────────────────────────────────────


def test_the_report_carries_every_required_field(report: ImpactReport) -> None:
    payload = report.model_dump()
    for field in ("from_model", "to_model", "n", "equivalent", "regressed",
                  "cost_delta", "token_delta", "worst_regressions"):
        assert field in payload, f"spec §5.9 requires {field}"
    assert payload["from_model"] == "gpt-a"
    assert payload["to_model"] == "gpt-b"


def test_counts_are_conserved(report: ImpactReport) -> None:
    assert report.n == 4
    assert report.equivalent == 1
    assert report.regressed == 2
    assert report.inconclusive == 1
    assert report.equivalent + report.regressed + report.inconclusive == report.n


def test_a_report_whose_counts_do_not_sum_is_refused() -> None:
    """The identity is enforced, not merely produced correctly by build_report."""
    with pytest.raises(ValueError, match="do not conserve"):
        ImpactReport(
            from_model="a", to_model="b", n=10, equivalent=1, regressed=1,
            inconclusive=0,
            cost_delta={"amount": 0, "contributing_runs": 0, "missing_runs": 0},
            token_delta={"amount": 0, "contributing_runs": 0, "missing_runs": 0},
        )


def test_an_unreplayable_run_is_inconclusive_not_a_pass() -> None:
    """Counting it as equivalent would report a clean result for a run nobody judged."""
    r = build_report([_run("bl-x", None)], from_model="a", to_model="b")
    assert r.inconclusive == 1
    assert r.equivalent == 0
    assert r.regressed == 0


# ── deltas stay interpretable ────────────────────────────────────────────────


def test_the_cost_delta_is_signed_and_counts_its_contributors(
    report: ImpactReport,
) -> None:
    # (90-100) + (260-200) + (150-150) = +50 minor units, over 3 of 4 runs.
    assert report.cost_delta.amount == 50
    assert report.cost_delta.currency == "EUR"
    assert report.cost_delta.contributing_runs == 3
    assert report.cost_delta.missing_runs == 1


def test_a_run_with_no_cost_data_is_not_treated_as_zero() -> None:
    """Summing a missing cost as 0 gives a delta that looks precise and is wrong."""
    corpus = [_run("bl-1", True, 0.0, 100, 90), _run("bl-2", True, 0.0)]

    r = build_report(corpus, from_model="a", to_model="b")

    assert r.cost_delta.amount == -10, "only the run with data contributes"
    assert r.cost_delta.contributing_runs == 1
    assert r.cost_delta.missing_runs == 1, "the gap must be visible"


def test_a_cheaper_model_gives_a_negative_delta() -> None:
    r = build_report([_run("bl-1", True, 0.0, 500, 300)], from_model="a", to_model="b")
    assert r.cost_delta.amount == -200


def test_mixing_currencies_is_refused_not_summed() -> None:
    """EUR minor units plus JPY minor units is a number with no meaning."""
    corpus = [
        RunOutcome(baseline_id="bl-1", equivalent=True,
                   cost_before=Money(amount_minor=100, currency="EUR"),
                   cost_after=Money(amount_minor=90, currency="EUR")),
        RunOutcome(baseline_id="bl-2", equivalent=True,
                   cost_before=Money(amount_minor=100, currency="JPY"),
                   cost_after=Money(amount_minor=90, currency="JPY")),
    ]
    with pytest.raises(ImpactError, match="mixes currencies"):
        build_report(corpus, from_model="a", to_model="b")


def test_the_token_delta_is_dimensionless(report: ImpactReport) -> None:
    # (900-1000) + (2400-2000) + (1500-1500) = +300
    assert report.token_delta.amount == 300
    assert report.token_delta.currency is None
    assert report.token_delta.contributing_runs == 3


# ── worst regressions ────────────────────────────────────────────────────────


def test_worst_regressions_are_ordered_worst_first(report: ImpactReport) -> None:
    assert [r.baseline_id for r in report.worst_regressions] == ["bl-3", "bl-2"]
    assert report.worst_regressions[0].distance == 0.8


def test_only_regressed_runs_appear(report: ImpactReport) -> None:
    listed = {r.baseline_id for r in report.worst_regressions}
    assert "bl-1" not in listed, "an equivalent run is not a regression"
    assert "bl-4" not in listed, "an inconclusive run is not a regression"


def test_the_list_is_bounded() -> None:
    corpus = [_run(f"bl-{i}", False, distance=i / 10) for i in range(10)]
    r = build_report(corpus, from_model="a", to_model="b", worst_n=3)

    assert len(r.worst_regressions) == 3
    assert r.regressed == 10, "bounding the list must not change the count"


# ── it decides nothing ───────────────────────────────────────────────────────


def test_the_report_offers_no_adoption_verdict(report: ImpactReport) -> None:
    """ADR-0147: NF-154 MUST NOT decide whether to adopt the new model."""
    payload = json.dumps(report.model_dump()).lower()
    for forbidden in ("recommend", "adopt", "verdict", "approved", "should_"):
        assert forbidden not in payload, f"report must not carry {forbidden!r}"


# ── validation, additivity ───────────────────────────────────────────────────


def test_an_empty_corpus_is_refused() -> None:
    """Zero regressions over zero runs reads as a clean result."""
    with pytest.raises(ImpactError, match="at least one run"):
        build_report([], from_model="a", to_model="b")


def test_a_negative_worst_n_is_refused() -> None:
    with pytest.raises(ImpactError, match="cannot be negative"):
        build_report(CORPUS, from_model="a", to_model="b", worst_n=-1)


def test_money_is_never_a_float() -> None:
    with pytest.raises(ValueError):
        Money(amount_minor=1.5, currency="EUR")  # type: ignore[arg-type]


def test_no_report_leaves_the_capsule_byte_identical() -> None:
    capsule = {"run_id": "r", "facets": {"other": {"x": 1}}}
    original = json.dumps(capsule, sort_keys=True)

    assert attach_facet(capsule, None) == capsule
    assert json.dumps(capsule, sort_keys=True) == original


def test_round_trip_through_a_capsule(report: ImpactReport) -> None:
    assert facet_from_capsule(attach_facet({"run_id": "r"}, report)) == report


def test_an_invalid_report_is_reported() -> None:
    with pytest.raises(ImpactError, match="invalid impact report"):
        facet_from_capsule({"facets": {"impact_report": {"from_model": "a"}}})
