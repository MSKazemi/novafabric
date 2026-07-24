# Copyright 2024 NovaFabric Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""ADR-0146 P1 — per-agent cost attribution (NF-141).

Tests are organised by the ADR's invariants, because those are what a
reviewer needs to be convinced of: G5 conservation, I-4 basis-never-laundered,
"absent is not zero", I-3 additive-first / fail-open, I-1 record-only.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from pydantic import ValidationError

from novafabric.cost import (
    AgentCost,
    ConservationError,
    CostAttributionFacet,
    CurrencyMismatchError,
    Money,
    RunTotal,
    UnapportionableError,
    apportion,
    attach_facet,
    build_facet,
    verify_conservation,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO_ROOT / "schemas" / "run-capsule.schema.json"
BASELINE = (
    REPO_ROOT / "tests" / "fixtures" / "model-provenance" / "valid-text-only-capsule.json"
)


# ── Golden fixtures ───────────────────────────────────────────────────────
#
# The two the ADR names: a single-agent capsule that stays valid, and a
# four-agent split that conserves. The four-agent numbers are the spec §4.1
# example, restated in integer minor units and millijoules — the same run,
# without the float representation that forced the spec's `epsilon`.

_FOUR_AGENT_TOTAL = RunTotal(
    cost=Money(amount_minor=41_200, currency="USD"),
    millijoules=1_830_000,
    tokens_in=41_200,
    tokens_out=9_800,
    calls=22,
)

_FOUR_AGENTS = [
    AgentCost(
        agent_id="planner",
        cost=Money(amount_minor=12_100, currency="USD"),
        millijoules=540_000,
        tokens_in=12_000,
        tokens_out=3_100,
        calls=6,
        basis="measured",
    ),
    AgentCost(
        agent_id="retriever",
        cost=Money(amount_minor=10_400, currency="USD"),
        millijoules=610_000,
        tokens_in=18_800,
        tokens_out=1_200,
        calls=9,
        basis="measured",
    ),
    AgentCost(
        agent_id="executor",
        cost=Money(amount_minor=14_800, currency="USD"),
        millijoules=520_000,
        tokens_in=8_100,
        tokens_out=4_300,
        calls=4,
        basis="measured",
    ),
    AgentCost(
        agent_id="validator",
        cost=Money(amount_minor=3_900, currency="USD"),
        millijoules=160_000,
        tokens_in=2_300,
        tokens_out=1_200,
        calls=3,
        basis="apportioned",
        apportionment_key="token_share",
    ),
]

_SINGLE_AGENT_TOTAL = RunTotal(
    cost=Money(amount_minor=730, currency="EUR"), tokens_in=900, tokens_out=210, calls=2
)

_SINGLE_AGENT = AgentCost(
    agent_id="solo",
    cost=Money(amount_minor=730, currency="EUR"),
    tokens_in=900,
    tokens_out=210,
    calls=2,
    basis="measured",
)


@pytest.fixture(scope="module")
def schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text())


@pytest.fixture
def capsule() -> dict[str, Any]:
    return json.loads(BASELINE.read_text())


def _check(facet: CostAttributionFacet, dimension: str) -> Any:
    return next(c for c in facet.conservation if c.dimension == dimension)


# ── G5: conservation ──────────────────────────────────────────────────────


def test_four_agent_split_conserves_on_every_dimension() -> None:
    """The ADR's golden fixture: Σ per-agent == run total, exactly."""
    facet = build_facet(_FOUR_AGENT_TOTAL, _FOUR_AGENTS)
    assert facet is not None
    for dimension in ("cost", "millijoules", "tokens_in", "tokens_out", "calls"):
        check = _check(facet, dimension)
        assert check.attributed == check.total
        assert check.unattributed == 0
        assert check.ok is True


def test_single_agent_split_conserves() -> None:
    facet = build_facet(_SINGLE_AGENT_TOTAL, [_SINGLE_AGENT])
    assert facet is not None
    assert verify_conservation(facet) is True


def test_over_attribution_raises_rather_than_rescaling() -> None:
    """A violated invariant is the finding, not a number to be normalised.

    Quietly scaling the agents down to fit the total would delete the exact
    discrepancy the conservation check exists to reveal — a facet that always
    adds up proves nothing.
    """
    inflated = [*_FOUR_AGENTS[:3], _FOUR_AGENTS[3].model_copy(update={"calls": 99})]
    with pytest.raises(ConservationError) as excinfo:
        build_facet(_FOUR_AGENT_TOTAL, inflated)
    assert excinfo.value.dimension == "calls"
    assert excinfo.value.total == 22
    assert excinfo.value.attributed == 118


def test_over_attributed_money_raises_naming_the_currency() -> None:
    """Money gets its own check because it carries a unit the counts do not."""
    inflated = [
        *_FOUR_AGENTS[:3],
        _FOUR_AGENTS[3].model_copy(
            update={"cost": Money(amount_minor=99_999, currency="USD")}
        ),
    ]
    with pytest.raises(ConservationError) as excinfo:
        build_facet(_FOUR_AGENT_TOTAL, inflated)
    assert excinfo.value.dimension == "cost"
    assert excinfo.value.unit == "USD minor units"


def test_conservation_error_is_not_a_value_error() -> None:
    """Named type, so a caller cannot mistake it for a shape complaint."""
    assert not issubclass(ConservationError, ValueError)


def test_unattributed_remainder_is_reported_not_hidden() -> None:
    """Spend that ties to no agent is a gap, not an error and not a zero."""
    partial = [_FOUR_AGENTS[0], _FOUR_AGENTS[1]]
    facet = build_facet(_FOUR_AGENT_TOTAL, partial)
    assert facet is not None
    cost = _check(facet, "cost")
    assert cost.attributed == 22_500
    assert cost.unattributed == 18_700
    assert cost.ok is True
    assert verify_conservation(facet) is True


def test_zero_total_run_conserves() -> None:
    """A run that spent nothing is attributable, and Σ 0 == 0."""
    facet = build_facet(
        RunTotal(cost=Money(amount_minor=0, currency="USD"), calls=0),
        [
            AgentCost(
                agent_id="idle",
                cost=Money(amount_minor=0, currency="USD"),
                calls=0,
                basis="measured",
            )
        ],
    )
    assert facet is not None
    assert _check(facet, "cost").total == 0
    assert _check(facet, "cost").unattributed == 0
    assert verify_conservation(facet) is True


def test_dimension_absent_from_the_run_total_is_not_conserved_against_zero() -> None:
    """No captured whole means nothing to conserve — not a zero whole.

    Treating an uncaptured run total as 0 would turn every attributed agent
    into a conservation failure on a dimension nobody measured.
    """
    facet = build_facet(RunTotal(calls=2), [_SINGLE_AGENT])
    assert facet is not None
    assert {c.dimension for c in facet.conservation} == {"calls"}


def test_cross_currency_attribution_is_refused() -> None:
    """Adding EUR minor units to JPY yields a number, not a sum of money."""
    mixed = _SINGLE_AGENT.model_copy(
        update={"cost": Money(amount_minor=730, currency="JPY")}
    )
    with pytest.raises(CurrencyMismatchError):
        build_facet(_SINGLE_AGENT_TOTAL, [mixed])


# ── G5: verification of an untrusted facet ────────────────────────────────


def test_tampered_agent_figure_fails_verification() -> None:
    """The builder cannot be trusted to have run over someone else's capsule."""
    facet = build_facet(_FOUR_AGENT_TOTAL, _FOUR_AGENTS)
    assert facet is not None
    facet.by_agent[0].calls = 1
    assert verify_conservation(facet) is False


def test_facet_with_no_conservation_block_does_not_verify() -> None:
    """An unchecked split is the case the verifier exists to surface."""
    facet = CostAttributionFacet(run_total=_SINGLE_AGENT_TOTAL, by_agent=[_SINGLE_AGENT])
    assert verify_conservation(facet) is False


def test_forged_ok_flag_does_not_survive_re_derivation() -> None:
    facet = build_facet(_FOUR_AGENT_TOTAL, _FOUR_AGENTS)
    assert facet is not None
    facet.conservation[0].attributed = 0
    assert verify_conservation(facet) is False


# ── Apportionment: no unit lost, none invented ────────────────────────────


def test_non_dividing_total_still_sums_to_the_total() -> None:
    """100 across 3 does not divide; the remainder must land somewhere."""
    shares = apportion(100, {"a": 1, "b": 1, "c": 1})
    assert sum(shares.values()) == 100
    assert shares == {"a": 34, "b": 33, "c": 33}


def test_remainder_assignment_is_deterministic_across_key_order() -> None:
    """Two verifiers re-deriving one capsule must not disagree."""
    forward = apportion(100, {"a": 1, "b": 1, "c": 1})
    reversed_order = apportion(100, {"c": 1, "b": 1, "a": 1})
    assert forward == reversed_order


def test_remainder_goes_to_the_largest_fractional_share_first() -> None:
    shares = apportion(10, {"big": 7, "small": 3})
    assert shares == {"big": 7, "small": 3}
    shares = apportion(7, {"big": 5, "small": 2})
    # 5.0 and 2.0 exactly — no remainder to place.
    assert shares == {"big": 5, "small": 2}
    shares = apportion(8, {"big": 5, "small": 2})
    # 5.714… and 2.285…; the .714 remainder outranks the .285.
    assert shares == {"big": 6, "small": 2}


def test_apportioning_zero_gives_every_agent_zero() -> None:
    assert apportion(0, {"a": 3, "b": 1}) == {"a": 0, "b": 0}
    assert apportion(0, {"a": 0, "b": 0}) == {"a": 0, "b": 0}
    assert apportion(0, {}) == {}


def test_all_zero_key_refuses_rather_than_inventing_an_even_split() -> None:
    """An all-zero key states nothing about who spent what.

    Splitting evenly over it would manufacture a `basis` the capture never
    recorded; the fail-open move is to leave the agents unattributed.
    """
    with pytest.raises(UnapportionableError):
        apportion(500, {"a": 0, "b": 0})


def test_apportionment_rejects_negative_inputs() -> None:
    with pytest.raises(UnapportionableError):
        apportion(-1, {"a": 1})
    with pytest.raises(UnapportionableError):
        apportion(10, {"a": -1})
    with pytest.raises(UnapportionableError):
        apportion(10, {})


def test_apportioned_split_feeds_a_conserving_facet() -> None:
    """End to end: the split of a non-dividing total conserves in the facet."""
    weights = {"planner": 1, "retriever": 1, "executor": 1}
    shares = apportion(100, weights)
    facet = build_facet(
        RunTotal(cost=Money(amount_minor=100, currency="USD")),
        [
            AgentCost(
                agent_id=agent,
                cost=Money(amount_minor=minor, currency="USD"),
                basis="apportioned",
                apportionment_key="token_share",
            )
            for agent, minor in shares.items()
        ],
    )
    assert facet is not None
    assert _check(facet, "cost").unattributed == 0


# ── I-4: basis is never laundered ─────────────────────────────────────────


def test_apportioned_agent_stays_apportioned_in_the_facet() -> None:
    facet = build_facet(_FOUR_AGENT_TOTAL, _FOUR_AGENTS)
    assert facet is not None
    validator = next(a for a in facet.by_agent if a.agent_id == "validator")
    assert validator.basis == "apportioned"
    assert validator.apportionment_key == "token_share"


def test_basis_is_required_on_every_agent() -> None:
    """A figure with no stated basis cannot be told from a measurement."""
    with pytest.raises(ValidationError):
        AgentCost(agent_id="x", calls=1)  # type: ignore[call-arg]


def test_basis_admits_no_third_value() -> None:
    with pytest.raises(ValidationError):
        AgentCost(agent_id="x", basis="guessed")  # type: ignore[arg-type]


# ── Absent is not zero ────────────────────────────────────────────────────


def test_unmeasured_dimension_is_absent_from_the_serialised_facet() -> None:
    """Reporting an unknown cost as 0 understates a bill.

    The serialised agent must carry no `cost` key at all, rather than
    `cost: 0`, which an auditor would read as "this agent spent nothing".
    """
    facet = build_facet(
        RunTotal(cost=Money(amount_minor=500, currency="USD"), calls=4),
        [AgentCost(agent_id="unmetered", calls=4, basis="measured")],
    )
    assert facet is not None
    dumped = facet.model_dump(exclude_none=True)
    assert "cost" not in dumped["by_agent"][0]
    assert dumped["conservation"][0]["unattributed"] == 500


def test_money_is_integer_minor_units_not_a_float() -> None:
    """A float amount cannot sum exactly, so the invariant would be theatre."""
    assert Money(amount_minor=41_200, currency="USD").amount_minor == 41_200
    with pytest.raises(ValidationError):
        Money(amount_minor=-1, currency="USD")
    with pytest.raises(ValidationError):
        Money(amount_minor=1, currency="usd")
    with pytest.raises(ValidationError):
        Money(amount_minor=1, currency="DOLLAR")


# ── I-3: additive-first / fail-open ───────────────────────────────────────


def test_capsule_with_nothing_to_attribute_is_untouched() -> None:
    """Byte-identical to a capsule captured before this feature existed."""
    original = {"run_id": "r", "facets": {"existing": {"a": 1}}}
    assert attach_facet(original, build_facet(RunTotal(), [])) == original


def test_single_agent_local_run_writes_no_facet() -> None:
    """The ADR's fail-open edge case: no team graph, no split to record."""
    assert build_facet(_SINGLE_AGENT_TOTAL, []) is None


def test_attach_does_not_mutate_the_input_capsule() -> None:
    original: dict[str, Any] = {"run_id": "r"}
    attach_facet(original, build_facet(_FOUR_AGENT_TOTAL, _FOUR_AGENTS))
    assert original == {"run_id": "r"}


def test_attach_preserves_sibling_facets() -> None:
    out = attach_facet(
        {"run_id": "r", "facets": {"existing": {"a": 1}}},
        build_facet(_FOUR_AGENT_TOTAL, _FOUR_AGENTS),
    )
    assert out["facets"]["existing"] == {"a": 1}
    assert "cost_attribution" in out["facets"]


def test_agents_are_ordered_for_byte_identical_output() -> None:
    facet = build_facet(_FOUR_AGENT_TOTAL, _FOUR_AGENTS)
    assert facet is not None
    assert [a.agent_id for a in facet.by_agent] == [
        "executor",
        "planner",
        "retriever",
        "validator",
    ]


def test_facet_carries_a_schema_version() -> None:
    out = attach_facet({"run_id": "r"}, build_facet(_FOUR_AGENT_TOTAL, _FOUR_AGENTS))
    assert out["facets"]["cost_attribution"]["schema_version"]


# ── The real schema ───────────────────────────────────────────────────────


def test_four_agent_facet_validates_against_the_real_schema(
    schema: dict[str, Any], capsule: dict[str, Any]
) -> None:
    """Uses the shipped builder, not a hand-written dict (ADR-0196 D4).

    A hand-written dict is what let five earlier facet slices ship writing a
    capsule the schema rejected.
    """
    out = attach_facet(capsule, build_facet(_FOUR_AGENT_TOTAL, _FOUR_AGENTS))
    assert "cost_attribution" in out["facets"]
    jsonschema.validate(out, schema)


def test_single_agent_capsule_still_validates(
    schema: dict[str, Any], capsule: dict[str, Any]
) -> None:
    """The ADR's other golden fixture: single-agent stays valid."""
    out = attach_facet(capsule, build_facet(_SINGLE_AGENT_TOTAL, [_SINGLE_AGENT]))
    jsonschema.validate(out, schema)


def test_no_material_attach_is_a_no_op_and_still_validates(
    schema: dict[str, Any], capsule: dict[str, Any]
) -> None:
    out = attach_facet(capsule, build_facet(RunTotal(), []))
    assert out == capsule
    jsonschema.validate(out, schema)


# ── I-1: record-only ──────────────────────────────────────────────────────


def test_module_exposes_no_enforcement_surface() -> None:
    """Record-only is a property of the API, not just of the docs.

    If a block/enforce/throttle/gate entry point ever appears here, this
    fails — which is the point: I-1 should be structurally hard to violate.
    ADR-0146 P3 is explicit that even the quota work stays record-only.
    """
    import novafabric.cost as cost

    forbidden = {
        "block",
        "enforce",
        "reject",
        "refuse",
        "apply",
        "intervene",
        "throttle",
        "gate",
        "budget",
        "limit",
        "optimize",
    }
    assert forbidden.isdisjoint(
        {name.lower() for name in cost.__all__}
    ), "cost must not expose an enforcement entry point (ADR-0146 I-4)"


def test_facet_carries_no_verdict_field() -> None:
    """No `over_budget`, no `ok_to_proceed`: whether spend was acceptable is
    the operator's call, and a verdict field would invite an enforcer to read
    one out of an evidence record."""
    dumped = CostAttributionFacet(
        run_total=_FOUR_AGENT_TOTAL, by_agent=_FOUR_AGENTS
    ).model_dump()
    assert not {"over_budget", "verdict", "allowed", "blocked"} & set(dumped)
