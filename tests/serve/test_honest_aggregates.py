"""ADR-0234 D2/D3 — an aggregate that cannot be computed faithfully refuses.

The rule's whole value is that a rendered number can be trusted *because* the
untrustworthy ones are not rendered. ADR-0234 rejects the conventional
approximation-with-a-badge explicitly: a number on screen is read as a number
regardless of the badge beside it.

The load-bearing half is **"absent is not zero"** — and its converse, which these
tests pin just as hard: a count of nothing is legitimately `0`, and turning this
rule into "zero is suspicious" would be its own defect.
"""

from __future__ import annotations

from typing import Any

import pytest

from novafabric.serve.aggregates import (
    AggregateCondition,
    computable,
    refuse,
    worst,
)


def _refusal(condition: AggregateCondition = AggregateCondition.TRUNCATED_SOURCE) -> Any:
    return refuse(condition, reason="because", remedy="do this instead")


# ---------------------------------------------------------------------------
# D2 — a refusal carries no number
# ---------------------------------------------------------------------------


def test_a_refusal_puts_no_value_on_the_wire() -> None:
    """Not zero, not null. A value alongside computable=False is a number to render."""
    payload = _refusal().as_dict()
    assert payload["computable"] is False
    assert "value" not in payload, (
        "a refusal that still carries a value hands the caller something to render, "
        "which is the exact failure the rule exists to prevent"
    )


def test_a_computable_verdict_carries_its_value() -> None:
    payload = computable(42).as_dict()
    assert payload["computable"] is True
    assert payload["value"] == 42
    assert "condition" not in payload and "remedy" not in payload


def test_zero_is_a_perfectly_good_computable_value() -> None:
    """The converse of "absent is not zero", and just as important.

    "No runs failed in this bucket" is a measured fact. If this rule made zero
    suspicious it would refuse to report good news, and be switched off.
    """
    payload = computable(0).as_dict()
    assert payload["computable"] is True
    assert payload["value"] == 0


def test_there_is_no_condition_for_the_number_being_small() -> None:
    """A guard against this module growing into "zero looks wrong"."""
    names = {c.value for c in AggregateCondition}
    assert not {n for n in names if "zero" in n or "small" in n or "low" in n}


# ---------------------------------------------------------------------------
# D3 — a refusal states what would make it computable
# ---------------------------------------------------------------------------


def test_a_refusal_without_a_remedy_is_rejected_at_construction() -> None:
    """"Unavailable" is not a remedy — ADR-0234 names a bad message as the whole risk."""
    with pytest.raises(ValueError, match="what would make it computable"):
        refuse(AggregateCondition.TRUNCATED_SOURCE, reason="nope", remedy="")
    with pytest.raises(ValueError, match="what would make it computable"):
        refuse(AggregateCondition.TRUNCATED_SOURCE, reason="nope", remedy="   ")


def test_a_refusal_without_a_reason_is_rejected_at_construction() -> None:
    with pytest.raises(ValueError, match="must state a reason"):
        refuse(AggregateCondition.TRUNCATED_SOURCE, reason="", remedy="do this")


def test_every_condition_can_be_constructed_and_serialized() -> None:
    """All four D2 conditions (plus source-unavailable) are representable."""
    for condition in AggregateCondition:
        payload = refuse(condition, reason="r", remedy="m").as_dict()
        assert payload["condition"] == condition.value


def test_the_condition_set_is_exactly_what_the_adr_enumerates() -> None:
    """A fifth condition means the rule grew a case nobody decided."""
    assert {c.value for c in AggregateCondition} == {
        "unpushable_filter",
        "truncated_source",
        "absent_contributor",
        "tenant_unsafe_store",
        "source_unavailable",
    }


# ---------------------------------------------------------------------------
# Combining verdicts
# ---------------------------------------------------------------------------


def test_any_refusal_refuses_the_whole() -> None:
    assert worst([computable(1), _refusal(), computable(2)]).computable is False


def test_all_computable_stays_computable() -> None:
    assert worst([computable(1), computable(2)]).computable is True


def test_the_most_fundamental_refusal_is_the_one_reported() -> None:
    """Otherwise the message depends on evaluation order.

    An operator who fixes the reported cause only to meet a second one has been
    given a worse experience than a single accurate answer.
    """
    verdict = worst(
        [
            refuse(AggregateCondition.ABSENT_CONTRIBUTOR, reason="r", remedy="m"),
            refuse(AggregateCondition.SOURCE_UNAVAILABLE, reason="r", remedy="m"),
            refuse(AggregateCondition.TRUNCATED_SOURCE, reason="r", remedy="m"),
        ]
    )
    assert verdict.condition is AggregateCondition.SOURCE_UNAVAILABLE


def test_ordering_does_not_change_the_reported_refusal() -> None:
    a = refuse(AggregateCondition.TRUNCATED_SOURCE, reason="r", remedy="m")
    b = refuse(AggregateCondition.TENANT_UNSAFE_STORE, reason="r", remedy="m")
    assert worst([a, b]).condition is worst([b, a]).condition


def test_an_empty_sequence_has_nothing_to_refuse() -> None:
    assert worst([]).computable is True


def test_notes_survive_combination() -> None:
    merged = worst([computable(1, unpriced_calls=3), computable(2, calls=9)])
    assert merged.notes == {"unpriced_calls": 3, "calls": 9}


# ---------------------------------------------------------------------------
# The live defect: unpriced is not free
# ---------------------------------------------------------------------------


def test_an_unpriced_model_is_distinguishable_from_a_free_one() -> None:
    """The defect this slice exists to close, asserted at its source.

    ``_estimate_cost`` returns 0.0 for a model with no catalog price — a
    deliberate choice, because pricing must never fail a capture. Without a
    companion that says so, ``sum(cost_usd)`` reports an unpriced run as $0.00
    and an operator reads it as free.
    """
    from novafabric.cost.interceptor import CostInterceptor

    unknown = "acme-frontier-9000-does-not-exist"
    assert CostInterceptor._estimate_cost(unknown, 1_000, 1_000) == 0.0
    assert CostInterceptor.is_priced(unknown) is False, (
        "if this returns True the aggregate cannot tell unpriced from free"
    )


def test_a_known_model_is_priced() -> None:
    """Non-vacuity: a coverage check that always says False proves nothing."""
    from novafabric.cost.interceptor import CostInterceptor

    priced = [m for m in CostInterceptor.PRICE_TABLE if CostInterceptor.is_priced(m)]
    assert priced, "no model in the built-in price table reports as priced"


def test_the_clickhouse_schema_records_pricing_coverage() -> None:
    """`cost_usd` is non-nullable, so the flag is the only way to carry the fact."""
    from novafabric.cost import clickhouse_store

    assert "priced" in clickhouse_store._DDL_COST_EVENTS
    assert any("priced" in stmt for stmt in clickhouse_store._MIGRATIONS), (
        "existing deployments need the additive ALTER, not just the CREATE"
    )


def test_the_insert_columns_match_the_row_tuple() -> None:
    """A column added to one and not the other is a silent column-shift on insert."""
    import inspect
    import re

    source = inspect.getsource(
        __import__("novafabric.cost.clickhouse_store", fromlist=["x"]).ingest_capsule
    )
    columns = re.search(r"column_names=\[(.*?)\]", source, re.S)
    tuple_body = re.search(r"rows\.append\(\s*\((.*?)\)\s*\)", source, re.S)
    assert columns and tuple_body
    n_cols = len([c for c in columns.group(1).split(",") if c.strip()])
    n_vals = len([v for v in tuple_body.group(1).split(",") if v.strip()])
    assert n_cols == n_vals, f"{n_cols} columns but {n_vals} values per row"


# ---------------------------------------------------------------------------
# The endpoint: unavailable must not look like zero
# ---------------------------------------------------------------------------

pytest.importorskip("fastapi")


@pytest.fixture
def cost_client(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    from fastapi.testclient import TestClient

    from novafabric.serve.app import create_app

    monkeypatch.delenv("NOVA_CLICKHOUSE_URL", raising=False)
    capsules = tmp_path / "runs"
    capsules.mkdir()
    app = create_app(
        token="cost-token-0123456789abcdef",
        capsule_dir=capsules,
        static_mounted_by_caller=True,
    )
    with TestClient(app) as client:
        yield client


HEADERS = {"host": "127.0.0.1:4321"}
TOKEN = "cost-token-0123456789abcdef"


def test_cost_summary_refuses_when_the_store_is_not_configured(cost_client: Any) -> None:
    """It used to return `{"costs": {}}` and call that degrading gracefully.

    An empty result is indistinguishable from "these runs cost nothing" — the
    same class as ADR-0229's finding that an empty list reads as "no results"
    rather than "cannot answer".
    """
    body = cost_client.get(
        f"/api/runs/cost-summary?run_ids=r1,r2&token={TOKEN}", headers=HEADERS
    ).json()
    assert body["costs"] == {}
    aggregate = body["aggregate"]
    assert aggregate["computable"] is False
    assert aggregate["condition"] == "source_unavailable"
    assert "value" not in aggregate
    assert "NOVA_CLICKHOUSE_URL" in aggregate["remedy"], "the remedy must be actionable"


def test_cost_summary_keeps_its_previous_shape(cost_client: Any) -> None:
    """Additive: `costs` is unchanged, so existing consumers keep working."""
    body = cost_client.get(
        f"/api/runs/cost-summary?run_ids=r1&token={TOKEN}", headers=HEADERS
    ).json()
    assert "costs" in body and isinstance(body["costs"], dict)


def test_an_unavailable_store_refuses_even_for_an_empty_id_list(cost_client: Any) -> None:
    """Availability is a property of the system, not of the question asked.

    Returning a computable ``{}`` here would be *literally* true — you asked
    about no runs, so the answer is nothing — and would still leave the caller
    believing the cost store answered them. The store's state is the more useful
    fact, so it is checked first and reported.
    """
    body = cost_client.get(
        f"/api/runs/cost-summary?run_ids=&token={TOKEN}", headers=HEADERS
    ).json()
    assert body["aggregate"]["computable"] is False
    assert body["aggregate"]["condition"] == "source_unavailable"


def test_an_empty_id_list_is_computable_when_the_store_is_available(
    cost_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half: with a store configured, asking about nothing is answerable.

    Pinned so the ordering above cannot quietly become "always refuse", which
    would make the rule useless by making it unconditional.
    """
    monkeypatch.setenv("NOVA_CLICKHOUSE_URL", "http://127.0.0.1:1/nova")
    body = cost_client.get(
        f"/api/runs/cost-summary?run_ids=&token={TOKEN}", headers=HEADERS
    ).json()
    assert body["aggregate"]["computable"] is True
    assert body["aggregate"]["value"] == {}
