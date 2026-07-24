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

"""ADR-0146 P1 — per-agent joule split (NF-141, energy side).

The energy half of the same slice: regroup ADR-0093 receipts by agent, in
integer millijoules, so the split can be conserved exactly by the
cost-attribution facet.
"""

from __future__ import annotations

from novafabric.cost import AgentCost, Money, RunTotal, build_facet
from novafabric.energy._attribution import (
    joules_to_millijoules,
    split_receipts_by_agent,
)
from novafabric.energy._receipt import (
    ActionKind,
    ActionRef,
    AttributionMethod,
    CarbonAccountingMode,
    Confidence,
    EnergyReceipt,
    Hardware,
    MeasurementScope,
    MeasurementSource,
    unavailable_receipt,
)

_AGENT_OF = {
    "call-1": "planner",
    "call-2": "planner",
    "call-3": "retriever",
    "call-4": None,
}


def _receipt(
    action_id: str,
    joules: float,
    confidence: Confidence = Confidence.MEASURED,
) -> EnergyReceipt:
    return EnergyReceipt(
        action_ref=ActionRef(kind=ActionKind.MODEL_CALL, id=action_id),
        run_id="run-a",
        capsule_id="cap-a",
        measured_joules=joules,
        measurement_source=MeasurementSource.RAPL_PKG,
        measurement_scope=MeasurementScope.PER_NODE,
        confidence=confidence,
        attribution_method=AttributionMethod.DIRECT_COUNTER,
        carbon_g_co2e=None,
        grid_intensity_g_per_kwh=None,
        carbon_accounting_mode=CarbonAccountingMode.UNKNOWN,
        hardware=Hardware(node_id="n1"),
        generated_at="2026-07-20T10:00:00Z",
    )


def _lookup(receipt: EnergyReceipt) -> str | None:
    return _AGENT_OF.get(receipt.action_ref.id)


# ── Millijoule conversion ─────────────────────────────────────────────────


def test_conversion_is_exact_on_decimal_values() -> None:
    """`round(0.0295 * 1000)` is 29, not 30: the float error is multiplied.

    Going via Decimal(str(...)) keeps the millijoule figure equal to what an
    auditor reading the receipt would compute by hand.
    """
    assert joules_to_millijoules(0.0295) == 30
    assert joules_to_millijoules(1.5) == 1500
    assert joules_to_millijoules(0.0) == 0


def test_conversion_rounds_half_up_not_to_even() -> None:
    assert joules_to_millijoules(0.0005) == 1
    assert joules_to_millijoules(0.0015) == 2


# ── Grouping ──────────────────────────────────────────────────────────────


def test_receipts_group_by_agent_and_conserve() -> None:
    receipts = [
        _receipt("call-1", 1.5),
        _receipt("call-2", 2.25),
        _receipt("call-3", 0.75),
    ]
    split = split_receipts_by_agent(receipts, agent_of=_lookup)
    assert split.by_agent == {"planner": 3750, "retriever": 750}
    assert split.unattributed_millijoules == 0
    assert split.total_millijoules == 4500
    assert sum(split.by_agent.values()) == split.total_millijoules
    assert split.basis == "measured"


def test_unmapped_action_lands_in_unattributed_not_on_an_arbitrary_agent() -> None:
    receipts = [_receipt("call-1", 1.0), _receipt("call-4", 4.0)]
    split = split_receipts_by_agent(receipts, agent_of=_lookup)
    assert split.by_agent == {"planner": 1000}
    assert split.unattributed_millijoules == 4000
    assert split.total_millijoules == 5000


def test_unavailable_receipt_contributes_to_nothing_not_to_zero() -> None:
    """An agent whose node had no counter is not an agent that burned nothing."""
    receipts = [
        _receipt("call-1", 1.0),
        unavailable_receipt(
            action_ref=ActionRef(kind=ActionKind.MODEL_CALL, id="call-3"),
            run_id="run-a",
            capsule_id="cap-a",
            node_id="n1",
            counters_available=[],
        ),
    ]
    split = split_receipts_by_agent(receipts, agent_of=_lookup)
    assert "retriever" not in split.by_agent
    assert split.total_millijoules == 1000


def test_one_apportioned_receipt_downgrades_the_whole_basis() -> None:
    """Grading an estimate as `measured` would launder it (I-4)."""
    receipts = [
        _receipt("call-1", 1.0),
        _receipt("call-3", 1.0, confidence=Confidence.APPORTIONED),
    ]
    assert split_receipts_by_agent(receipts, agent_of=_lookup).basis == "apportioned"


def test_empty_split_is_not_graded_measured() -> None:
    split = split_receipts_by_agent([], agent_of=_lookup)
    assert split.by_agent == {}
    assert split.total_millijoules == 0
    assert split.basis == "apportioned"


def test_split_feeds_a_conserving_cost_attribution_facet() -> None:
    """The two halves of NF-141 wire together without either importing the
    other: the caller carries the number across."""
    receipts = [_receipt("call-1", 1.5), _receipt("call-3", 0.5)]
    split = split_receipts_by_agent(receipts, agent_of=_lookup)
    facet = build_facet(
        RunTotal(
            cost=Money(amount_minor=0, currency="USD"),
            millijoules=split.total_millijoules,
        ),
        [
            AgentCost(agent_id=agent, millijoules=milli, basis=split.basis)  # type: ignore[arg-type]
            for agent, milli in split.by_agent.items()
        ],
    )
    assert facet is not None
    energy = next(c for c in facet.conservation if c.dimension == "millijoules")
    assert energy.attributed == energy.total == 2000
    assert energy.unattributed == 0
