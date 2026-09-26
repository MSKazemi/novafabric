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

"""ADR-0170 P2 — NF-383 liability-attribution chain + shared guard controls.

Attribution evidence, never fault: every test here either proves the chain is
walkable ordered evidence or proves a verdict cannot be smuggled into it.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from pydantic import ValidationError

from novafabric.risk_transfer import (
    DeterminationFieldRejectedError,
    InvalidDecimalError,
    InvalidLiabilityChainError,
    InvalidReferenceError,
    LiabilityEdge,
    PaymentSecretRejectedError,
    RiskTransferFacet,
    UnboundedFieldError,
    UnsourcedContributionError,
    attach_facet,
    attributed_parties,
    build_facet,
    build_liability_chain,
    digest_artifact,
)
from novafabric.risk_transfer._guard import (
    MAX_FREE_STRING,
    normalise_key,
    reject_determination_fields,
    to_decimal,
    validate_ref,
    validate_refs,
)
from novafabric.risk_transfer.liability import MAX_CHAIN, check_chain

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "risk-transfer"
SCHEMA_PATH = REPO_ROOT / "schemas" / "run-capsule.schema.json"
BASELINE = REPO_ROOT / "tests" / "fixtures" / "model-provenance" / "valid-text-only-capsule.json"


def _chain(name: str) -> list[dict[str, Any]]:
    doc = json.loads((FIXTURES / name).read_text())
    return list(doc["liability_chain"])


def _ref(label: str) -> str:
    return digest_artifact(label)


# ── Golden fixtures ───────────────────────────────────────────────────────


def test_golden_valid_chain_attaches_and_validates_against_real_schema() -> None:
    chain = build_liability_chain(_chain("liability-chain-valid.json"))
    assert chain is not None
    capsule = json.loads(BASELINE.read_text())
    out = attach_facet(capsule, build_facet(liability_chain=chain))
    jsonschema.validate(out, json.loads(SCHEMA_PATH.read_text()))
    recorded = out["facets"]["risk_transfer"]["liability_chain"]
    assert [e["role"] for e in recorded] == ["principal", "deployer", "model_provider", "agent"]
    # Round-trips through the facet model unchanged.
    assert RiskTransferFacet.model_validate(out["facets"]["risk_transfer"]).liability_chain


def test_golden_fault_key_is_rejected() -> None:
    with pytest.raises(DeterminationFieldRejectedError) as info:
        build_liability_chain(_chain("liability-chain-invalid-fault-key.json"))
    assert info.value.marker == "fault"
    assert "At-Fault" in info.value.path


def test_golden_dangling_acted_as_is_rejected() -> None:
    with pytest.raises(InvalidLiabilityChainError, match="names no party"):
        build_liability_chain(_chain("liability-chain-invalid-dangling.json"))


def test_golden_unsourced_contribution_is_rejected() -> None:
    with pytest.raises(UnsourcedContributionError):
        build_liability_chain(_chain("liability-chain-invalid-unsourced.json"))


# ── Attribution, never fault ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "key",
    [
        "fault",
        "at_fault",
        "AtFault",
        "is-liable",
        "liability_share",
        "Liability Percent",
        "verdict",
        "negligence",
        "blame",
        "responsible_party",
        "payout",
        "claim_decision",
        "is_covered",
        "policy_responds",
        "damages",
        "service_credit",
    ],
)
def test_verdict_shaped_extra_keys_are_rejected_after_normalisation(key: str) -> None:
    entry = {"role": "vendor", "party_ref": _ref("v"), key: True}
    with pytest.raises(DeterminationFieldRejectedError):
        LiabilityEdge.model_validate(entry)


def test_nested_verdict_key_is_rejected() -> None:
    entry = {"role": "vendor", "party_ref": _ref("v"), "notes": {"deep": [{"FaultFinding": 1}]}}
    with pytest.raises(DeterminationFieldRejectedError):
        LiabilityEdge.model_validate(entry)


def test_edge_has_no_fault_field() -> None:
    fields = {normalise_key(f) for f in LiabilityEdge.model_fields}
    assert not any(m in f for f in fields for m in ("fault", "liable", "verdict"))


@pytest.mark.parametrize("marker", ["guilty", "at_fault", "liable", "", "RECORDED"])
def test_contribution_marker_is_a_closed_set(marker: str) -> None:
    with pytest.raises(ValidationError):
        LiabilityEdge(
            role="vendor",
            party_ref=_ref("v"),
            basis_ref=_ref("b"),
            contribution_marker=marker,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("role", ["insurer", "court", "Principal", "victim"])
def test_role_is_a_closed_set(role: str) -> None:
    with pytest.raises(ValidationError):
        LiabilityEdge(role=role, party_ref=_ref("p"))  # type: ignore[arg-type]


def test_disputed_requires_a_basis() -> None:
    with pytest.raises(UnsourcedContributionError):
        LiabilityEdge(role="vendor", party_ref=_ref("v"), contribution_marker="disputed")


def test_none_marker_needs_no_basis() -> None:
    edge = LiabilityEdge(role="agent", party_ref=_ref("a"))
    assert edge.contribution_marker == "none"


def test_attributed_parties_reads_back_assertions_in_order() -> None:
    chain = build_liability_chain(_chain("liability-chain-valid.json"))
    assert chain is not None
    parties = attributed_parties(chain)
    assert parties == [chain[1].party_ref, chain[2].party_ref]


# ── Chain structure ───────────────────────────────────────────────────────


def test_empty_chain_is_not_emitted() -> None:
    assert build_liability_chain([]) is None
    assert build_facet(liability_chain=[]) is None
    with pytest.raises(InvalidLiabilityChainError):
        check_chain([])


def test_order_is_preserved() -> None:
    entries = list(reversed(_chain("liability-chain-valid.json")))
    chain = build_liability_chain(entries)
    assert chain is not None
    assert [e.role for e in chain] == ["agent", "model_provider", "deployer", "principal"]


def test_self_acting_party_is_rejected() -> None:
    p = _ref("p")
    with pytest.raises(InvalidLiabilityChainError, match="itself"):
        LiabilityEdge(role="agent", party_ref=p, acted_as_ref=p)


def test_cycle_is_rejected() -> None:
    a, b, c = _ref("a"), _ref("b"), _ref("c")
    entries = [
        {"role": "agent", "party_ref": a, "acted_as_ref": b},
        {"role": "operator", "party_ref": b, "acted_as_ref": c},
        {"role": "deployer", "party_ref": c, "acted_as_ref": a},
    ]
    with pytest.raises(InvalidLiabilityChainError, match="cycle"):
        build_liability_chain(entries)


def test_diamond_is_not_a_cycle() -> None:
    a, b, c, d = (_ref(x) for x in "abcd")
    entries = [
        {"role": "principal", "party_ref": d},
        {"role": "deployer", "party_ref": b, "acted_as_ref": d},
        {"role": "operator", "party_ref": c, "acted_as_ref": d},
        {"role": "agent", "party_ref": a, "acted_as_ref": b},
        {"role": "integrator", "party_ref": a, "acted_as_ref": c},
    ]
    chain = build_liability_chain(entries)
    assert chain is not None and len(chain) == 5


def test_duplicate_role_for_same_party_is_rejected() -> None:
    p = _ref("p")
    with pytest.raises(InvalidLiabilityChainError, match="repeats"):
        build_liability_chain([{"role": "vendor", "party_ref": p}] * 2)


def test_same_party_in_two_roles_is_allowed() -> None:
    p = _ref("p")
    chain = build_liability_chain(
        [{"role": "vendor", "party_ref": p}, {"role": "integrator", "party_ref": p}]
    )
    assert chain is not None and len(chain) == 2


def test_chain_length_is_capped() -> None:
    entries = [{"role": "third_party", "party_ref": _ref(str(i))} for i in range(MAX_CHAIN + 1)]
    with pytest.raises(InvalidLiabilityChainError, match="cap"):
        build_liability_chain(entries)


def test_accepts_model_instances() -> None:
    edge = LiabilityEdge(role="agent", party_ref=_ref("a"))
    assert build_liability_chain([edge]) == [edge]


def test_facet_level_chain_invariants_hold_on_model_validate() -> None:
    p = _ref("p")
    with pytest.raises(InvalidLiabilityChainError):
        RiskTransferFacet.model_validate(
            {"liability_chain": [{"role": "agent", "party_ref": p, "acted_as_ref": _ref("x")}]}
        )


def test_facet_rejects_verdict_key_at_facet_level() -> None:
    with pytest.raises(DeterminationFieldRejectedError):
        RiskTransferFacet.model_validate({"fault_finding": {"party": "x"}})


def test_facet_still_rejects_payment_secrets_in_chain_extras() -> None:
    entry = {"role": "vendor", "party_ref": _ref("v"), "note": "4111111111111111"}
    with pytest.raises(PaymentSecretRejectedError):
        RiskTransferFacet.model_validate({"liability_chain": [entry]})


# ── References (fullmatch, not ^…$) ───────────────────────────────────────


@pytest.mark.parametrize(
    "bad",
    [
        "sha256:" + "a" * 64 + "\n",
        "sha256:" + "A" * 64,
        "sha256:" + "a" * 63,
        "Acme Corp",
        "https://example.test/party",
        " sha256:" + "a" * 64,
    ],
)
def test_party_ref_must_be_exact_digest(bad: str) -> None:
    with pytest.raises(InvalidReferenceError):
        LiabilityEdge(role="vendor", party_ref=bad)


def test_trailing_newline_digest_rejected_in_p1_objects_too() -> None:
    from novafabric.risk_transfer import IncidentLoss

    with pytest.raises(InvalidReferenceError):
        IncidentLoss(incident_bundle_ref="sha256:" + "a" * 64 + "\n")


def test_validate_ref_rejects_non_string() -> None:
    with pytest.raises(InvalidReferenceError):
        validate_ref(42)  # type: ignore[arg-type]


def test_validate_refs_is_bounded() -> None:
    with pytest.raises(UnboundedFieldError):
        validate_refs([_ref("x")] * 3, limit=2)


# ── Guard bounds ──────────────────────────────────────────────────────────


def test_oversize_extra_string_is_rejected() -> None:
    entry = {"role": "vendor", "party_ref": _ref("v"), "note": "x" * (MAX_FREE_STRING + 1)}
    with pytest.raises(UnboundedFieldError):
        LiabilityEdge.model_validate(entry)


def test_oversize_key_list_and_depth_are_rejected() -> None:
    with pytest.raises(UnboundedFieldError):
        reject_determination_fields({"k" * (MAX_FREE_STRING + 1): 1})
    with pytest.raises(UnboundedFieldError):
        reject_determination_fields({"l": list(range(1000))})
    deep: dict[str, Any] = {}
    cur = deep
    for _ in range(20):
        cur["n"] = {}
        cur = cur["n"]
    with pytest.raises(UnboundedFieldError):
        reject_determination_fields(deep)


def test_legitimate_field_names_pass_the_guard() -> None:
    reject_determination_fields(
        {
            "covered_event_kind": "x",
            "liability_chain": [],
            "matched_exclusions": [],
            "contribution_marker": "recorded",
            "declared_by": "operator",
            "breach": True,
            "condition_met": False,
        }
    )


@pytest.mark.parametrize("bad", [1.5, True, "NaN", "inf", "abc", object(), "1" * 100])
def test_to_decimal_refuses_inexact_or_non_finite(bad: object) -> None:
    with pytest.raises(InvalidDecimalError):
        to_decimal(bad)


def test_to_decimal_accepts_exact_forms() -> None:
    assert to_decimal("99.9") == Decimal("99.9")
    assert to_decimal(7) == Decimal(7)
    assert to_decimal(Decimal("0.05")) == Decimal("0.05")


def test_to_decimal_rejects_huge_exponent_text() -> None:
    with pytest.raises(InvalidDecimalError):
        to_decimal(Decimal("1" * 70))
