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

"""ADR-0163 P3 — negotiated agreement (NF-316) + invoice/receipt binding (NF-319).

Plus the two P1 hardenings this slice needed: an exact-digest match (no
trailing newline) and strict integer minor units (no float, bool or string).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from novafabric.settlement import (
    AGREEMENT_FIELD,
    INVOICE_FIELD,
    AgreementRecord,
    FinalityRecord,
    InvalidIdentityRefError,
    InvalidReferenceError,
    InvoiceMatchLaunderedError,
    InvoiceReceipt,
    MalformedAgreementError,
    MalformedInvoiceError,
    Money,
    NonCanonicalTermsError,
    PaymentSecretRejectedError,
    SettlementFacet,
    agreement_from_facet,
    attach_agreement,
    attach_invoice,
    bind_invoice,
    build_agreement,
    build_facet,
    compare_to_settlement,
    digest_terms,
    invoice_from_facet,
    verify_agreement,
    verify_invoice,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "settlement"
TERMS: dict[str, Any] = json.loads((FIXTURES / "valid-agreement-terms.json").read_text())

INVOICE_DOC = b"%PDF-1.7 invoice 2026-0713 total EUR 120.00"
EUR_120 = Money(amount_minor=12000, currency="EUR")


def d256(text: str | bytes) -> str:
    raw = text.encode() if isinstance(text, str) else text
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _load(name: str) -> SettlementFacet:
    return SettlementFacet.model_validate(json.loads((FIXTURES / name).read_text()))


def _settled(amount: Money | None = EUR_120, state: str = "settled") -> SettlementFacet:
    facet = build_facet(
        protocol="x402",
        settlement_ref=d256("c"),
        amount=amount,
        finality=FinalityRecord.model_validate(
            {
                "state": state,
                "finality_source": "declared",
                "observed_at": "2026-07-13T10:00:00Z",
            }
        ),
    )
    assert facet is not None
    return facet


def _agreement(**kw: Any) -> AgreementRecord:
    base: dict[str, Any] = {
        "agreement_ref": d256("agreement-artifact"),
        "terms_digest": digest_terms(TERMS),
        "parties": ["agent:acme-buyer", "agent:travel-broker"],
        "agreed_at": "2026-07-13T09:58:00Z",
        "negotiation_transcript_ref": d256("negotiation-transcript"),
    }
    base.update(kw)
    return build_agreement(**base)


# ── NF-316 agreement ──────────────────────────────────────────────────────


def test_golden_agreement_reads_and_reverifies() -> None:
    record = agreement_from_facet(_load("valid-p3-facet.json"))
    assert record is not None
    result = verify_agreement(
        record,
        artifact="agreement-artifact",
        terms=TERMS,
        transcript="negotiation-transcript",
    )
    assert result.ok
    assert result.interpreted is False


def test_unchecked_agreement_is_not_ok() -> None:
    """Fail-closed: a record nobody re-derived is not a verified one."""
    result = verify_agreement(_agreement())
    assert result.agreement_ref_ok is None and result.terms_digest_ok is None
    assert not result.ok


def test_recorded_transcript_must_be_rechecked_for_ok() -> None:
    result = verify_agreement(_agreement(), artifact="agreement-artifact", terms=TERMS)
    assert result.transcript_bound and result.transcript_ok is None
    assert not result.ok


def test_agreement_without_transcript_can_verify() -> None:
    record = _agreement(negotiation_transcript_ref=None)
    result = verify_agreement(record, artifact="agreement-artifact", terms=TERMS)
    assert not result.transcript_bound
    assert result.ok


def test_tampered_terms_fail_verification() -> None:
    result = verify_agreement(
        _agreement(), artifact="agreement-artifact", terms={**TERMS, "nights": 3}
    )
    assert result.terms_digest_ok is False and not result.ok


def test_wrong_artifact_and_transcript_fail_verification() -> None:
    result = verify_agreement(_agreement(), artifact="other", terms=TERMS, transcript="other")
    assert result.agreement_ref_ok is False and result.transcript_ok is False


def test_terms_that_cannot_be_canonicalised_verify_false_not_raise() -> None:
    result = verify_agreement(_agreement(), terms={"price": 120.0})
    assert result.terms_digest_ok is False


def test_terms_digest_is_key_order_independent() -> None:
    reordered = dict(reversed(list(TERMS.items())))
    assert digest_terms(reordered) == digest_terms(TERMS)


@pytest.mark.parametrize(
    "terms",
    [
        {"price": 120.0},
        {1: "x"},
        {"x": object()},
        {"x": {"nested": [1.5]}},
    ],
)
def test_non_canonical_terms_are_refused(terms: Any) -> None:
    with pytest.raises(NonCanonicalTermsError):
        digest_terms(terms)


def test_terms_depth_and_size_are_bounded() -> None:
    deep: dict[str, Any] = {}
    node = deep
    for _ in range(40):
        node["n"] = {}
        node = node["n"]
    with pytest.raises(NonCanonicalTermsError, match="deeper"):
        digest_terms(deep)
    with pytest.raises(NonCanonicalTermsError, match="more than"):
        digest_terms({"items": list(range(20_000))})


def test_terms_carrying_a_pan_are_refused() -> None:
    with pytest.raises(PaymentSecretRejectedError):
        digest_terms({"card": "4111 1111 1111 1111"})


def test_terms_accept_tuples_bools_and_none() -> None:
    assert digest_terms({"a": (1, 2), "b": True, "c": None}).startswith("sha256:")


def test_agreement_needs_two_distinct_parties() -> None:
    with pytest.raises(ValidationError):
        _agreement(parties=["agent:acme-buyer"])
    with pytest.raises(ValidationError, match="twice"):
        _agreement(parties=["agent:a", "agent:a"])
    with pytest.raises(ValidationError):
        _agreement(parties="agent:a,agent:b")
    with pytest.raises(ValidationError, match="more than"):
        _agreement(parties=[f"agent:{i}" for i in range(65)])


def test_golden_one_party_fixture_is_malformed() -> None:
    with pytest.raises(MalformedAgreementError, match="parties"):
        agreement_from_facet(_load("invalid-agreement-one-party.json"))


def test_party_with_credential_userinfo_is_refused() -> None:
    with pytest.raises(InvalidIdentityRefError):
        _agreement(parties=["agent:a", "https://bob:hunter2@agents.example/b"])


@pytest.mark.parametrize("field", ["agreement_ref", "terms_digest", "negotiation_transcript_ref"])
def test_agreement_refs_must_be_digests(field: str) -> None:
    with pytest.raises(InvalidReferenceError):
        _agreement(**{field: "https://contracts.example/42"})


@pytest.mark.parametrize("instant", ["2026-07-13", "2026-07-13T09:58:00", "yesterday", 5])
def test_agreed_at_must_be_an_offset_instant(instant: Any) -> None:
    with pytest.raises(ValidationError):
        _agreement(agreed_at=instant)


def test_attach_agreement_round_trips() -> None:
    facet = _settled()
    out = attach_agreement(facet, _agreement())
    assert agreement_from_facet(facet) is None
    assert agreement_from_facet(out) == _agreement()


def test_agreement_extra_field_named_for_a_secret_is_rejected() -> None:
    with pytest.raises(PaymentSecretRejectedError):
        AgreementRecord.model_validate({**_agreement().model_dump(), "private_key": "k"})


# ── NF-319 invoice / receipt ──────────────────────────────────────────────


def _bind(**kw: Any) -> InvoiceReceipt:
    base: dict[str, Any] = {
        "document_ref": d256(INVOICE_DOC),
        "document_type": "invoice",
        "issuer_ref": "agent:travel-broker",
        "total_amount": EUR_120,
        "settlement": _settled(),
        "resolver": lambda ref: INVOICE_DOC,
    }
    base.update(kw)
    return bind_invoice(**base)


def test_bound_matching_invoice_matches_settlement() -> None:
    record = _bind()
    assert record.unbound is False
    assert record.matches_settlement is True
    assert record.mismatches == []
    assert verify_invoice(record, _settled(), document=INVOICE_DOC).ok


def test_golden_invoice_fixture_verifies() -> None:
    facet = _load("valid-p3-facet.json")
    record = invoice_from_facet(facet)
    assert record is not None
    result = verify_invoice(record, facet)
    assert result.ok and result.document_rechecked is None


def test_unresolvable_document_is_unbound_never_fatal() -> None:
    record = _bind(resolver=lambda ref: None)
    assert record.unbound is True
    assert record.matches_settlement is None


def test_no_resolver_means_unbound() -> None:
    assert _bind(resolver=None).unbound is True


def test_resolver_that_raises_degrades_to_unbound() -> None:
    def boom(ref: str) -> bytes:
        raise OSError("store offline")

    assert _bind(resolver=boom).unbound is True


def test_resolver_returning_other_bytes_is_unbound() -> None:
    assert _bind(resolver=lambda ref: b"a different document").unbound is True


def test_amount_mismatch_is_recorded_false() -> None:
    record = _bind(total_amount=Money(amount_minor=12500, currency="EUR"))
    assert record.matches_settlement is False
    assert record.mismatches == ["amount_mismatch"]


def test_currency_mismatch_suppresses_amount_comparison() -> None:
    record = _bind(total_amount=Money(amount_minor=99, currency="USD"))
    assert record.mismatches == ["currency_mismatch"]


def test_unsettled_finality_is_a_mismatch() -> None:
    record = _bind(settlement=_settled(state="captured"))
    assert record.matches_settlement is False
    assert record.mismatches == ["not_settled"]


def test_unbound_document_can_still_disagree() -> None:
    record = _bind(resolver=None, total_amount=Money(amount_minor=1, currency="EUR"))
    assert record.unbound and record.matches_settlement is False


def test_nothing_to_compare_is_none_not_true() -> None:
    assert compare_to_settlement("invoice", EUR_120, None) == (None, [])
    no_amount = _settled(amount=None)
    assert compare_to_settlement("receipt", EUR_120, no_amount) == (None, [])
    no_finality = build_facet(protocol="x402", amount=EUR_120)
    assert compare_to_settlement("receipt", EUR_120, no_finality) == (None, [])


def test_credit_note_is_never_compared_against_a_settlement() -> None:
    record = _bind(document_type="credit_note", total_amount=Money(amount_minor=1, currency="USD"))
    assert record.matches_settlement is None and record.mismatches == []


@pytest.mark.parametrize(
    "fields",
    [
        {"matches_settlement": True, "unbound": True},
        {"matches_settlement": True, "unbound": False, "mismatches": ["amount_mismatch"]},
        {"matches_settlement": None, "mismatches": ["not_settled"]},
        {"matches_settlement": False, "mismatches": []},
    ],
)
def test_laundered_verdicts_are_refused_on_every_path(fields: dict[str, Any]) -> None:
    raw = {
        "document_ref": d256(INVOICE_DOC),
        "document_type": "receipt",
        "issuer_ref": "agent:shop",
        "total_amount": {"amount_minor": 12000, "currency": "EUR"},
        **fields,
    }
    with pytest.raises(InvoiceMatchLaunderedError):
        InvoiceReceipt.model_validate(raw)


def test_golden_unbound_match_fixture_is_refused() -> None:
    facet = _load("invalid-invoice-unbound-match.json")
    with pytest.raises(InvoiceMatchLaunderedError):
        invoice_from_facet(facet)


def test_verify_detects_a_tampered_verdict() -> None:
    record = _bind()
    # The facet changes under the record: the settled amount no longer matches.
    moved = _settled(amount=Money(amount_minor=1, currency="EUR"))
    result = verify_invoice(record, moved)
    assert not result.consistent and not result.ok


def test_verify_with_wrong_document_bytes_fails_closed() -> None:
    result = verify_invoice(_bind(), _settled(), document=b"forged")
    assert result.document_rechecked is False and result.unbound and not result.ok


def test_verify_of_unbound_record_is_not_ok() -> None:
    result = verify_invoice(_bind(resolver=None), _settled())
    assert result.consistent and not result.ok


def test_invoice_refs_are_validated() -> None:
    with pytest.raises(InvalidReferenceError):
        _bind(document_ref="sha256:" + "0" * 64 + "\n")
    with pytest.raises(InvalidIdentityRefError):
        _bind(issuer_ref="issuer with spaces")


def test_malformed_stored_invoice_is_reported() -> None:
    facet = SettlementFacet.model_validate(
        {"protocol": "x402", INVOICE_FIELD: {"document_type": "invoice"}}
    )
    with pytest.raises(MalformedInvoiceError):
        invoice_from_facet(facet)
    assert invoice_from_facet(_settled()) is None


def test_attach_invoice_round_trips() -> None:
    record = _bind()
    out = attach_invoice(_settled(), record)
    assert invoice_from_facet(out) == record
    assert out.model_dump()[INVOICE_FIELD]["unbound"] is False


def test_a_facet_with_only_an_agreement_is_built() -> None:
    facet = build_facet(protocol="ap2", extra={AGREEMENT_FIELD: _agreement().model_dump()})
    assert facet is not None


# ── P1 hardenings exercised by P3 ─────────────────────────────────────────


@pytest.mark.parametrize("bad", [5.0, True, "500"])
def test_money_minor_units_are_strict_ints(bad: object) -> None:
    with pytest.raises(ValidationError):
        Money(amount_minor=bad, currency="EUR")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        Money.model_validate_json(json.dumps({"amount_minor": bad, "currency": "EUR"}))


def test_facet_digest_refs_reject_a_trailing_newline() -> None:
    with pytest.raises(InvalidReferenceError):
        build_facet(protocol="ap2", settlement_ref="sha256:" + "a" * 64 + "\n")
