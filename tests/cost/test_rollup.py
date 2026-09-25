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
"""ADR-0146 P2 / NF-142 — acted-as cost rollup (report-only).

Acceptance criteria exercised here:

- each granter's ``subtree_cost`` includes every grantee's spend and the root
  subtree equals the run total exactly (Decimal, no epsilon);
- no chain → ``basis: partial`` + ``no_chain`` finding, never an exception;
- cycles, self-grants, broken linkage and multi-granter principals become
  findings, and no cost is ever counted twice or dropped (conservation
  identity always holds);
- unattributed and unchained cost are reported explicitly;
- bounded input (grants, chains, principals, depth, amount) and deterministic
  output.
"""

from __future__ import annotations

import json
import random
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from novafabric.cost import rollup as rollup_mod
from novafabric.cost.rollup import (
    RollupBoundsError,
    RollupInputError,
    RollupReport,
    build_rollup,
    parse_attribution,
    parse_delegation,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "cost-rollup"


def _load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


def _grant(a: str, b: str, **extra: Any) -> dict[str, Any]:
    return {"granter_id": a, "grantee_id": b, **extra}


def _facet(
    costs: dict[str, int | None],
    total: int | None,
    *,
    currency: str = "USD",
    basis: str = "measured",
) -> dict[str, Any]:
    agents = []
    for agent_id, amount in costs.items():
        entry: dict[str, Any] = {"agent_id": agent_id, "basis": basis}
        if amount is not None:
            entry["cost"] = {"amount_minor": amount, "currency": currency}
        agents.append(entry)
    attributed = sum(v for v in costs.values() if v is not None)
    doc: dict[str, Any] = {"run_total": {}, "by_agent": agents, "conservation": []}
    if total is not None:
        doc["run_total"]["cost"] = {"amount_minor": total, "currency": currency}
        doc["conservation"] = [
            {
                "dimension": "cost",
                "unit": f"{currency} minor units",
                "total": total,
                "attributed": attributed,
                "unattributed": total - attributed,
            }
        ]
    return doc


def _run(delegation: Any, attribution: Any) -> RollupReport:
    return build_rollup(parse_delegation(delegation), parse_attribution(attribution))


def _codes(report: RollupReport) -> list[str]:
    return [f.code for f in report.findings]


def _hop(report: RollupReport, agent: str) -> Any:
    return next(h for h in report.hops if h.agent_id == agent)


def _identity_holds(report: RollupReport) -> bool:
    c = report.conservation
    assert c.run_total_cost is not None and c.unattributed_cost is not None
    return c.run_total_cost == c.root_subtree_cost + c.unchained_cost + c.unattributed_cost


# ── Golden fixtures ───────────────────────────────────────────────────────


class TestGoldenValid:
    def test_subtrees_roll_up_and_conserve(self) -> None:
        report = _run(_load("delegation_valid.json"), _load("attribution_valid.json"))
        assert report.basis == "measured"
        assert report.findings == []
        assert [h.agent_id for h in report.hops] == [
            "user:alice",
            "planner",
            "executor",
            "validator",
            "retriever",
        ]
        planner = _hop(report, "planner")
        assert planner.self_cost == Decimal("1.21")
        assert planner.subtree_cost == Decimal("4.12")
        assert planner.grantees == ["executor", "retriever"]
        assert _hop(report, "executor").subtree_cost == Decimal("1.87")
        alice = _hop(report, "user:alice")
        assert alice.self_cost is None  # the human root spent nothing attributed
        assert alice.depth == 0 and alice.granter is None
        c = report.conservation
        assert c.roots == ["user:alice"]
        assert c.root_subtree_cost == c.run_total_cost == Decimal("4.12")
        assert c.ok is True
        assert c.unattributed_cost == Decimal("0") and c.unchained_cost == Decimal("0")
        assert report.signatures_verified is False
        assert "Record-only" in report.record_only

    def test_deterministic_under_shuffled_input(self) -> None:
        deleg = _load("delegation_valid.json")
        att = _load("attribution_valid.json")
        baseline = _run(deleg, att).model_dump_json()
        rng = random.Random(7)
        for _ in range(5):
            shuffled = json.loads(json.dumps(deleg))
            rng.shuffle(shuffled["chains"])
            shuffled_att = json.loads(json.dumps(att))
            rng.shuffle(shuffled_att["facets"]["cost_attribution"]["by_agent"])
            assert _run(shuffled, shuffled_att).model_dump_json() == baseline


class TestGoldenInvalid:
    def test_cycle_is_a_finding_not_a_crash(self) -> None:
        report = _run(_load("delegation_cycle_invalid.json"), _load("attribution_valid.json"))
        assert report.basis == "partial"
        assert _codes(report) == ["cycle"]
        assert report.findings[0].principals == ["executor", "planner", "validator"]
        # Cyclic grants dropped; no cost double-counted or lost.
        assert _hop(report, "planner").subtree_cost == Decimal("2.25")
        assert report.conservation.root_subtree_cost == Decimal("4.12")
        assert report.conservation.ok is True
        assert _identity_holds(report)

    def test_broken_chain_drops_later_hops(self) -> None:
        report = _run(
            _load("delegation_broken_chain_invalid.json"), _load("attribution_valid.json")
        )
        assert report.basis == "partial"
        assert _codes(report) == ["broken_linkage", "unchained_agent"]
        assert [h.agent_id for h in report.hops] == ["user:alice", "planner"]
        assert [u.agent_id for u in report.unchained] == ["executor", "retriever", "validator"]
        c = report.conservation
        assert c.root_subtree_cost == Decimal("1.21")
        assert c.unchained_cost == Decimal("2.91")
        assert c.ok is False
        assert _identity_holds(report)

    def test_invalid_attribution_refused(self) -> None:
        with pytest.raises(RollupInputError):
            parse_attribution(_load("attribution_invalid.json"))


# ── Degradation and findings ──────────────────────────────────────────────


class TestFindings:
    def test_no_chain_is_partial(self) -> None:
        report = _run({"grants": []}, _facet({"a": 10, "b": 20}, 30))
        assert report.basis == "partial"
        assert _codes(report) == ["no_chain"]
        assert report.hops == []
        assert report.conservation.unchained_cost == Decimal("0.30")
        assert report.conservation.ok is False
        assert _identity_holds(report)

    def test_single_linear_chain_shape(self) -> None:
        doc = {"grants": [_grant("u", "a"), _grant("a", "b")]}
        report = _run(doc, _facet({"a": 10, "b": 20}, 30))
        assert report.basis == "measured"
        assert _hop(report, "a").subtree_cost == Decimal("0.30")
        assert _hop(report, "b").depth == 2

    def test_public_key_mismatch_breaks_linkage(self) -> None:
        doc = {
            "grants": [
                _grant("u", "a", grantee_public_key="aa"),
                _grant("a", "b", granter_public_key="bb"),
            ]
        }
        report = _run(doc, _facet({"a": 10, "b": 20}, 30))
        assert "broken_linkage" in _codes(report)
        assert "public key mismatch" in report.findings[0].detail

    def test_self_delegation_ignored(self) -> None:
        doc = {"grants": [_grant("u", "a"), _grant("a", "a")]}
        report = _run(doc, _facet({"a": 10}, 10))
        assert _codes(report) == ["self_delegation"]
        assert _hop(report, "a").grantees == []
        assert report.conservation.ok is True
        assert report.basis == "partial"

    def test_multiple_granters_never_double_count(self) -> None:
        doc = {
            "chains": [
                {"grants": [_grant("u", "p"), _grant("p", "shared")]},
                {"grants": [_grant("u", "q"), _grant("q", "shared")]},
            ]
        }
        report = _run(doc, _facet({"p": 1, "q": 2, "shared": 100}, 103))
        assert _codes(report) == ["multiple_granters"]
        assert _hop(report, "shared").granter == "p"
        assert _hop(report, "p").subtree_cost == Decimal("1.01")
        assert _hop(report, "q").subtree_cost == Decimal("0.02")
        assert report.conservation.root_subtree_cost == Decimal("1.03")
        assert report.conservation.ok is True

    def test_unattributed_cost_is_explicit(self) -> None:
        doc = {"grants": [_grant("u", "a")]}
        report = _run(doc, _facet({"a": 10, "b": None}, 50))
        c = report.conservation
        assert c.unattributed_cost == Decimal("0.40")
        assert c.ok is False
        assert report.basis == "partial"
        unchained = {u.agent_id: u.self_cost for u in report.unchained}
        assert unchained == {"b": None}
        assert _identity_holds(report)

    def test_no_run_total(self) -> None:
        report = _run({"grants": [_grant("u", "a")]}, _facet({"a": 10}, None))
        assert "no_run_total" in _codes(report)
        assert report.conservation.run_total_cost is None
        assert report.conservation.unattributed_cost is None
        assert report.conservation.ok is False
        assert report.conservation.currency == "USD"

    def test_no_costs_anywhere(self) -> None:
        report = _run({"grants": [_grant("u", "a")]}, _facet({"a": None}, None))
        assert report.conservation.currency is None
        assert _hop(report, "a").self_cost is None

    def test_apportioned_basis_propagates(self) -> None:
        report = _run({"grants": [_grant("u", "a")]}, _facet({"a": 10}, 10, basis="apportioned"))
        assert report.basis == "apportioned"

    def test_tampered_facet_flagged(self) -> None:
        facet = _facet({"a": 10}, 10)
        facet["conservation"][0]["attributed"] = 9
        facet["conservation"][0]["unattributed"] = 1
        report = _run({"grants": [_grant("u", "a")]}, facet)
        assert "attribution_not_conserved" in _codes(report)

    def test_long_cycle_is_iterative(self) -> None:
        # One-hop chains forming a 3000-node ring: Tarjan must not recurse.
        n = 3000
        chains = [
            [rollup_mod.GrantEdge(granter_id=f"n{i:05d}", grantee_id=f"n{(i + 1) % n:05d}")]
            for i in range(n)
        ]
        report = build_rollup(chains, parse_attribution(_facet({}, None)))
        assert _codes(report) == ["cycle", "no_run_total"]
        assert len(report.findings[0].principals) == n
        assert all(h.depth == 0 for h in report.hops)


# ── Money ─────────────────────────────────────────────────────────────────


class TestMoney:
    def test_decimal_has_no_float_drift(self) -> None:
        report = _run(
            {"grants": [_grant("u", "a"), _grant("a", "b")]}, _facet({"a": 10, "b": 20}, 30)
        )
        assert _hop(report, "a").subtree_cost == Decimal("0.30")
        assert str(_hop(report, "a").subtree_cost) == "0.30"
        assert report.conservation.ok is True

    @pytest.mark.parametrize(
        ("currency", "expected"), [("JPY", "500"), ("KWD", "0.500"), ("EUR", "5.00")]
    )
    def test_minor_unit_exponent(self, currency: str, expected: str) -> None:
        report = _run({"grants": [_grant("u", "a")]}, _facet({"a": 500}, 500, currency=currency))
        assert str(_hop(report, "a").self_cost) == expected

    def test_currency_mismatch_refused(self) -> None:
        facet = _facet({"a": 10}, 10)
        facet["by_agent"][0]["cost"]["currency"] = "EUR"
        with pytest.raises(RollupInputError, match="cross-currency"):
            _run({"grants": [_grant("u", "a")]}, facet)

    def test_mixed_currencies_without_total_refused(self) -> None:
        facet = _facet({"a": 10, "b": 5}, None)
        facet["by_agent"][1]["cost"]["currency"] = "EUR"
        with pytest.raises(RollupInputError, match="several currencies"):
            _run({"grants": [_grant("u", "a")]}, facet)

    def test_duplicate_agent_refused(self) -> None:
        facet = _facet({"a": 10}, 20)
        facet["by_agent"].append(dict(facet["by_agent"][0]))
        with pytest.raises(RollupInputError, match="twice"):
            _run({"grants": [_grant("u", "a")]}, facet)


# ── Parsing and bounds ────────────────────────────────────────────────────


class TestParsing:
    @pytest.mark.parametrize(
        "doc",
        [
            [],
            {"grants": "x"},
            {"grants": ["x"]},
            {"grants": [{"granter_id": "a"}]},
            {"grants": [{"granter_id": "", "grantee_id": "b"}]},
            {"chains": "x"},
            {"chains": [{"nope": []}]},
            {"chains": ["x"]},
        ],
    )
    def test_bad_delegation(self, doc: Any) -> None:
        with pytest.raises(RollupInputError):
            parse_delegation(doc)

    def test_empty_chains_are_skipped(self) -> None:
        assert parse_delegation({"chains": [{"grants": []}]}) == []

    @pytest.mark.parametrize(
        "doc",
        [
            [],
            {"facets": {"other": {}}},
            {"cost_attribution": {"by_agent": []}},
            {"run_total": {}, "by_agent": [{"agent_id": "a", "basis": "guess"}]},
        ],
    )
    def test_bad_attribution(self, doc: Any) -> None:
        with pytest.raises(RollupInputError):
            parse_attribution(doc)

    def test_attribution_shapes(self) -> None:
        facet = _facet({"a": 1}, 1)
        for doc in (facet, {"cost_attribution": facet}, {"facets": {"cost_attribution": facet}}):
            assert parse_attribution(doc).run_total.cost is not None

    def test_chain_cap(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(rollup_mod, "MAX_CHAINS", 1)
        with pytest.raises(RollupBoundsError):
            parse_delegation({"chains": [{"grants": []}, {"grants": []}]})

    def test_grant_cap(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(rollup_mod, "MAX_GRANTS", 1)
        with pytest.raises(RollupBoundsError):
            parse_delegation({"grants": [_grant("a", "b"), _grant("b", "c")]})

    def test_agent_cap(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(rollup_mod, "MAX_PRINCIPALS", 1)
        with pytest.raises(RollupBoundsError):
            parse_attribution(_facet({"a": 1, "b": 1}, 2))

    def test_principal_cap(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(rollup_mod, "MAX_PRINCIPALS", 2)
        with pytest.raises(RollupBoundsError):
            build_rollup(
                parse_delegation({"grants": [_grant("a", "b"), _grant("b", "c")]}),
                parse_attribution(_facet({}, None)),
            )

    def test_amount_cap(self) -> None:
        with pytest.raises(RollupBoundsError):
            parse_attribution(_facet({"a": 10**30}, 10**30))

    def test_depth_cap(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(rollup_mod, "MAX_DEPTH", 3)
        chain = [_grant(f"n{i}", f"n{i + 1}") for i in range(6)]
        with pytest.raises(RollupBoundsError, match="depth"):
            _run({"grants": chain}, _facet({}, None))

    def test_deep_chain_within_cap(self) -> None:
        chain = [_grant(f"n{i:03d}", f"n{i + 1:03d}") for i in range(200)]
        report = _run({"grants": chain}, _facet({"n200": 7}, 7))
        assert report.hops[0].subtree_cost == Decimal("0.07")
        assert report.hops[-1].depth == 200
        assert report.conservation.ok is True

    def test_depth_cap_when_leaf_sorts_first(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(rollup_mod, "MAX_DEPTH", 3)
        chain = [_grant(f"n{i + 1}", f"n{i}") for i in reversed(range(6))]
        with pytest.raises(RollupBoundsError, match="depth"):
            _run({"grants": chain}, _facet({}, None))
