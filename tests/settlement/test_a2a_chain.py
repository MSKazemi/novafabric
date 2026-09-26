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

"""ADR-0163 P3 — A2A payment-provenance chain (NF-315).

Acceptance (spec §6): a two-hop agent-to-agent settlement walks ``acyclic`` and
``no_broken_parent``; NovaFabric records the hops and moves no value.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from novafabric.settlement import (
    CHAIN_FIELD,
    MAX_CHAIN_HOPS,
    A2APaymentHop,
    BrokenA2AChainError,
    InvalidIdentityRefError,
    InvalidReferenceError,
    MalformedA2AChainError,
    Money,
    PaymentSecretRejectedError,
    SettlementFacet,
    attach_chain,
    build_a2a_chain,
    build_facet,
    chain_from_facet,
    verify_a2a_chain,
    walk_back,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "settlement"


def d256(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def _hop(
    index: int,
    parent: int | None,
    *,
    payer: str | None = None,
    payee: str | None = None,
    currency: str = "EUR",
    amount: int = 1000,
) -> dict[str, Any]:
    return {
        "hop_index": index,
        "payer_agent_ref": payer if payer is not None else f"agent:a{index}",
        "payee_agent_ref": payee if payee is not None else f"agent:a{index + 1}",
        "amount": {"amount_minor": amount, "currency": currency},
        "settlement_ref": d256(f"confirmation-{index}"),
        "parent_hop": parent,
    }


def _chain(n: int) -> list[A2APaymentHop]:
    return [A2APaymentHop.model_validate(_hop(i, i - 1 if i else None)) for i in range(n)]


def _codes(hops: list[dict[str, Any]]) -> set[str]:
    parsed = [A2APaymentHop.model_validate(h) for h in hops]
    return {f.code for f in verify_a2a_chain(parsed).findings}


def _load(name: str) -> SettlementFacet:
    return SettlementFacet.model_validate(json.loads((FIXTURES / name).read_text()))


# ── Success ───────────────────────────────────────────────────────────────


def test_two_hop_chain_walks_acyclic_with_no_broken_parent() -> None:
    result = verify_a2a_chain(_chain(2))
    assert result.ok
    assert result.acyclic and result.no_broken_parent and result.linear
    assert result.hop_count == 2
    assert result.findings == []


def test_walk_records_that_no_value_moved() -> None:
    result = verify_a2a_chain(_chain(3))
    assert result.moved_value is False
    assert result.model_dump()["moved_value"] is False


def test_golden_valid_fixture_walks_clean() -> None:
    hops = chain_from_facet(_load("valid-p3-facet.json"))
    assert hops is not None and len(hops) == 3
    assert verify_a2a_chain(hops).ok


def test_build_returns_parsed_hops_for_a_valid_chain() -> None:
    hops = build_a2a_chain([_hop(0, None), _hop(1, 0)])
    assert [h.hop_index for h in hops] == [0, 1]


def test_attach_chain_round_trips_through_the_facet() -> None:
    facet = build_facet(protocol="x402", settlement_ref=d256("c"))
    assert facet is not None
    out = attach_chain(facet, _chain(2))
    assert out is not facet
    assert chain_from_facet(facet) is None  # input not mutated
    again = chain_from_facet(out)
    assert again is not None and verify_a2a_chain(again).ok
    assert out.model_dump()[CHAIN_FIELD][0]["parent_hop"] is None


def test_a_facet_with_only_a_chain_is_still_built() -> None:
    """Fail-open is for *absent* material; a chain is material."""
    facet = build_facet(protocol="x402", extra={CHAIN_FIELD: [_hop(0, None)]})
    assert facet is not None
    assert chain_from_facet(facet) is not None


def test_no_chain_reads_as_none_not_empty() -> None:
    facet = build_facet(protocol="ap2", settlement_ref=d256("c"))
    assert facet is not None
    assert chain_from_facet(facet) is None


# ── Broken links ──────────────────────────────────────────────────────────


def test_cycle_is_reported_as_forward_parent() -> None:
    codes = _codes([_hop(0, None), _hop(1, 2), _hop(2, 1)])
    assert "forward_parent" in codes
    result = verify_a2a_chain(
        [A2APaymentHop.model_validate(h) for h in [_hop(0, None), _hop(1, 2), _hop(2, 1)]]
    )
    assert not result.acyclic and not result.ok


def test_self_parent_is_a_cycle() -> None:
    assert "forward_parent" in _codes([_hop(0, None), _hop(1, 1)])


def test_first_hop_with_parent_is_broken() -> None:
    result = verify_a2a_chain([A2APaymentHop.model_validate(_hop(0, 0))])
    assert {f.code for f in result.findings} == {"first_hop_has_parent"}
    assert not result.no_broken_parent


def test_later_hop_without_parent_is_broken() -> None:
    assert _codes([_hop(0, None), _hop(1, None)]) == {"missing_parent"}


def test_parent_that_resolves_to_nothing_earlier_is_dangling() -> None:
    # hop_index 5 at position 1 (mismatch) points to parent 3, which never occurs.
    codes = _codes([_hop(0, None), _hop(5, 3, payer="agent:a1")])
    assert "dangling_parent" in codes
    assert "hop_index_mismatch" in codes


def test_fork_is_reported() -> None:
    codes = _codes([_hop(0, None), _hop(1, 0), _hop(2, 0, payer="agent:a1")])
    assert codes == {"fork"}


def test_duplicate_hop_index_is_reported() -> None:
    codes = _codes([_hop(0, None), _hop(1, 0), _hop(1, 0)])
    assert "duplicate_hop_index" in codes


def test_payer_must_be_parent_payee() -> None:
    codes = _codes([_hop(0, None), _hop(1, 0, payer="agent:stranger")])
    assert codes == {"payer_not_prior_payee"}


def test_currency_must_match_parent() -> None:
    hops = [_hop(0, None), _hop(1, 0, currency="USD")]
    result = verify_a2a_chain([A2APaymentHop.model_validate(h) for h in hops])
    assert {f.code for f in result.findings} == {"currency_mismatch"}
    assert not result.currency_consistent and not result.ok


def test_every_fault_is_reported_not_only_the_first() -> None:
    codes = _codes([_hop(0, 0), _hop(1, None), _hop(2, 1, currency="USD", payer="agent:x")])
    assert {"first_hop_has_parent", "missing_parent", "currency_mismatch"} <= codes
    assert "payer_not_prior_payee" in codes


def test_empty_chain_is_not_ok() -> None:
    result = verify_a2a_chain([])
    assert not result.ok
    assert [f.code for f in result.findings] == ["empty_chain"]


def test_over_long_chain_is_refused_without_walking() -> None:
    hop = A2APaymentHop.model_validate(_hop(0, None))
    result = verify_a2a_chain([hop] * (MAX_CHAIN_HOPS + 1))
    assert [f.code for f in result.findings] == ["chain_too_long"]
    assert not any(
        [result.ordered, result.no_broken_parent, result.acyclic, result.linear, result.ok]
    )


@pytest.mark.parametrize(
    "name,code",
    [
        ("invalid-chain-cycle.json", "forward_parent"),
        ("invalid-chain-fork.json", "fork"),
        ("invalid-chain-currency-mismatch.json", "currency_mismatch"),
    ],
)
def test_golden_invalid_fixtures_fail_the_walk(name: str, code: str) -> None:
    hops = chain_from_facet(_load(name))
    assert hops is not None
    result = verify_a2a_chain(hops)
    assert not result.ok
    assert code in {f.code for f in result.findings}


def test_build_refuses_a_broken_chain_with_every_finding() -> None:
    with pytest.raises(BrokenA2AChainError) as info:
        build_a2a_chain([_hop(0, None), _hop(1, None), _hop(2, 1, currency="USD")])
    codes = {f.code for f in info.value.verification.findings}
    assert codes == {"missing_parent", "currency_mismatch"}
    assert "missing_parent" in str(info.value)


def test_build_refuses_an_over_long_chain() -> None:
    with pytest.raises(MalformedA2AChainError):
        build_a2a_chain([_hop(0, None)] * (MAX_CHAIN_HOPS + 1))


# ── Shape, money and secrets ──────────────────────────────────────────────


def test_float_amount_is_refused() -> None:
    with pytest.raises(ValidationError):
        A2APaymentHop.model_validate(
            {**_hop(0, None), "amount": {"amount_minor": 10.0, "currency": "EUR"}}
        )


def test_golden_float_fixture_is_malformed() -> None:
    with pytest.raises(MalformedA2AChainError, match="amount"):
        chain_from_facet(_load("invalid-chain-float-amount.json"))


@pytest.mark.parametrize("bad", [True, 1.0, "1"])
def test_hop_index_and_parent_are_strict_ints(bad: object) -> None:
    with pytest.raises(ValidationError):
        A2APaymentHop.model_validate({**_hop(1, 0), "hop_index": bad})
    with pytest.raises(ValidationError):
        A2APaymentHop.model_validate({**_hop(1, 0), "parent_hop": bad})


def test_parent_hop_is_required_even_when_null() -> None:
    raw = _hop(0, None)
    del raw["parent_hop"]
    with pytest.raises(ValidationError):
        A2APaymentHop.model_validate(raw)


def test_pan_in_an_agent_ref_is_rejected() -> None:
    with pytest.raises(PaymentSecretRejectedError) as info:
        A2APaymentHop.model_validate(_hop(0, None, payee="agent:4111111111111111"))
    assert "4111111111111111" not in str(info.value)


def test_golden_pan_fixture_is_rejected_at_facet_load() -> None:
    raw = json.loads((FIXTURES / "invalid-chain-pan.json").read_text())
    with pytest.raises(PaymentSecretRejectedError):
        SettlementFacet.model_validate(raw)


def test_secret_named_extra_field_on_a_hop_is_rejected() -> None:
    with pytest.raises(PaymentSecretRejectedError):
        A2APaymentHop.model_validate({**_hop(0, None), "cvv": "123"})


@pytest.mark.parametrize(
    "ref",
    [
        "",
        " agent:x",
        "agent:x\n",
        "agent x",
        "a" * 257,
        "https://user:pw@agents.example/a",
        '{"inline":"artifact"}',
    ],
)
def test_bad_identity_refs_are_refused(ref: str) -> None:
    with pytest.raises(InvalidIdentityRefError):
        A2APaymentHop.model_validate(_hop(0, None, payer=ref))


def test_identity_ref_must_be_a_string() -> None:
    raw = _hop(0, None)
    raw["payer_agent_ref"] = 42
    with pytest.raises(InvalidIdentityRefError):
        A2APaymentHop.model_validate(raw)


@pytest.mark.parametrize(
    "ref", ["sha256:" + "a" * 64 + "\n", "sha256:" + "A" * 64, "https://x.example/c", 7]
)
def test_settlement_ref_must_be_an_exact_digest(ref: object) -> None:
    with pytest.raises(InvalidReferenceError) as info:
        A2APaymentHop.model_validate({**_hop(0, None), "settlement_ref": ref})
    assert "x.example" not in str(info.value)


def test_stored_chain_that_is_not_a_list_is_malformed() -> None:
    facet = SettlementFacet.model_validate({"protocol": "x402", CHAIN_FIELD: {"0": {}}})
    with pytest.raises(MalformedA2AChainError, match="list"):
        chain_from_facet(facet)


def test_stored_chain_over_the_cap_is_refused_before_parsing() -> None:
    facet = SettlementFacet.model_validate(
        {"protocol": "x402", CHAIN_FIELD: [{}] * (MAX_CHAIN_HOPS + 1)}
    )
    with pytest.raises(MalformedA2AChainError, match="refusing"):
        chain_from_facet(facet)


def test_already_parsed_hops_are_accepted_by_the_reader() -> None:
    hops = _chain(2)
    facet = SettlementFacet.model_construct(protocol="x402")
    facet.__pydantic_extra__ = {CHAIN_FIELD: hops}
    assert chain_from_facet(facet) == hops


# ── walk_back (display) ───────────────────────────────────────────────────


def test_walk_back_returns_newest_first_and_honours_depth() -> None:
    hops = _chain(4)
    assert [h.hop_index for h in walk_back(hops, 10)] == [3, 2, 1, 0]
    assert [h.hop_index for h in walk_back(hops, 2)] == [3, 2]
    assert walk_back(hops, 0) == []
    assert walk_back([], 3) == []


def test_walk_back_terminates_on_a_cycle() -> None:
    raw = [_hop(0, None), _hop(1, 2), _hop(2, 1)]
    hops = [A2APaymentHop.model_validate(h) for h in raw]
    # hop 2 -> parent 1 (earlier, followed) -> parent 2 (later, not followed).
    assert [h.hop_index for h in walk_back(hops, 100)] == [2, 1]


def test_chain_module_exposes_no_value_movement() -> None:
    import novafabric.settlement.chain as chain

    public = {name.lower() for name in dir(chain) if not name.startswith("_")}
    assert not public & {"pay", "transfer", "move", "release", "settle", "refund"}


def test_money_in_a_hop_is_integer_minor_units() -> None:
    hop = A2APaymentHop.model_validate(_hop(0, None, amount=16240))
    assert hop.amount == Money(amount_minor=16240, currency="EUR")
    assert isinstance(hop.amount.amount_minor, int)
