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

"""ADR-0163 P1 — settlement facet + reference binding (NF-311).

Tests are organised by the ADR's invariants, because those are what a
reviewer needs to be convinced of: I-1 record-only, I-2 secret-free,
I-3 additive-first / fail-open, I-4 records-never-determines.
"""

from __future__ import annotations

import hashlib

import pytest
from pydantic import ValidationError

from novafabric.settlement import (
    InvalidReferenceError,
    Money,
    PaymentSecretRejectedError,
    SettlementFacet,
    attach_facet,
    build_facet,
    digest_artifact,
    reject_payment_secrets,
    verify_ref_binding,
)

MANDATE = '{"type":"PaymentMandate","max_amount":"150.00","currency":"EUR"}'
CONFIRMATION = '{"msg":"sese.025","status":"SETT","amount":"162.40"}'

MANDATE_REF = digest_artifact(MANDATE)
CONFIRMATION_REF = digest_artifact(CONFIRMATION)
ROOT_REF = digest_artifact("capsule-root")

#: A Luhn-valid test PAN (the ISO/IEC 7812 Visa test number; not a real card).
TEST_PAN = "4111111111111111"
#: A mod-97-valid IBAN from the ISO 13616 registry examples.
TEST_IBAN = "GB82WEST12345698765432"
#: A NovaFabric API key of the repo's own `nvfk_` shape (ADR-0193).
TEST_API_KEY = "nvfk_abcd1234_" + "z" * 40


def _facet(**kw: object) -> SettlementFacet:
    base: dict[str, object] = {
        "protocol": "ap2",
        "protocol_version": "0.2",
        "mandate_ref": MANDATE_REF,
        "settlement_ref": CONFIRMATION_REF,
        "bound_root": ROOT_REF,
    }
    base.update(kw)
    return SettlementFacet(**base)  # type: ignore[arg-type]


# ── Reference binding ─────────────────────────────────────────────────────


def test_digest_is_sha256_of_the_artifact_with_algorithm_prefix() -> None:
    expected = hashlib.sha256(MANDATE.encode()).hexdigest()
    assert digest_artifact(MANDATE) == f"sha256:{expected}"


def test_digest_accepts_bytes_and_str_identically() -> None:
    assert digest_artifact(MANDATE) == digest_artifact(MANDATE.encode())


def test_binding_verifies_against_the_bound_artifact() -> None:
    facet = _facet()
    assert verify_ref_binding(facet.mandate_ref, MANDATE) is True
    assert verify_ref_binding(facet.settlement_ref, CONFIRMATION) is True
    # A confirmation altered by one character no longer binds.
    assert verify_ref_binding(facet.settlement_ref, CONFIRMATION + " ") is False


def test_mandate_and_confirmation_do_not_bind_to_each_other() -> None:
    facet = _facet()
    assert verify_ref_binding(facet.mandate_ref, CONFIRMATION) is False


def test_unbound_reference_does_not_verify() -> None:
    """A facet with no reference is the case the verifier exists to surface.

    Returning True for a missing ref would let exactly the facets with
    nothing to check sail through a binding check.
    """
    assert verify_ref_binding(None, MANDATE) is False
    assert verify_ref_binding("", MANDATE) is False


@pytest.mark.parametrize("field", ["mandate_ref", "settlement_ref", "bound_root"])
def test_reference_must_be_a_digest_not_an_artifact(field: str) -> None:
    """Inlining the artifact is the mistake D1 exists to prevent."""
    with pytest.raises(InvalidReferenceError):
        _facet(**{field: MANDATE})


@pytest.mark.parametrize(
    "bad_ref",
    [
        "https://psp.example/confirmations/42",  # a URI does not bind (see D1 note)
        "sha256:deadbeef",  # truncated digest
        "sha1:" + "a" * 40,  # wrong algorithm
        "SHA256:" + "A" * 64,  # wrong case
        MANDATE_REF + "x",  # over-long
    ],
)
def test_non_digest_references_are_refused(bad_ref: str) -> None:
    with pytest.raises(InvalidReferenceError):
        _facet(mandate_ref=bad_ref)


# ── I-2: secret-free ──────────────────────────────────────────────────────


def test_card_number_is_rejected_not_stored() -> None:
    """A PAN must raise, not be silently dropped or redacted.

    A caller that passed a card number has a broken payment integration and
    is holding that PAN elsewhere too; a quiet `[REDACTED]` would tell it
    everything is fine.
    """
    with pytest.raises(PaymentSecretRejectedError) as excinfo:
        _facet(pan_observed=TEST_PAN)
    assert TEST_PAN not in str(excinfo.value)


def test_card_number_in_an_ordinary_field_value_is_rejected() -> None:
    """The value shape is caught even when the field name is innocent."""
    with pytest.raises(PaymentSecretRejectedError):
        _facet(merchant_note=f"charged card {TEST_PAN} at checkout")


@pytest.mark.parametrize("separator", [" ", "-"])
def test_separated_card_number_is_rejected(separator: str) -> None:
    grouped = separator.join([TEST_PAN[i : i + 4] for i in range(0, 16, 4)])
    with pytest.raises(PaymentSecretRejectedError):
        _facet(merchant_note=grouped)


def test_card_number_hidden_in_an_integer_field_is_rejected() -> None:
    """`amount_minor` is exactly the int field an integration bug fills."""
    with pytest.raises(PaymentSecretRejectedError):
        _facet(amount=Money(amount_minor=int(TEST_PAN), currency="EUR"))


@pytest.mark.parametrize("field", ["cvv", "cvc", "card_cvv2", "csc"])
def test_cvv_field_is_rejected(field: str) -> None:
    """CVV is key-driven: three digits are indistinguishable from any number."""
    with pytest.raises(PaymentSecretRejectedError):
        _facet(**{field: "123"})


def test_cvv_field_is_rejected_even_when_empty() -> None:
    """The field's presence is the bug; its value is beside the point."""
    with pytest.raises(PaymentSecretRejectedError):
        _facet(cvv=None)


def test_iban_is_rejected() -> None:
    with pytest.raises(PaymentSecretRejectedError) as excinfo:
        _facet(payee_account=TEST_IBAN)
    assert TEST_IBAN not in str(excinfo.value)


def test_api_key_is_rejected() -> None:
    with pytest.raises(PaymentSecretRejectedError) as excinfo:
        _facet(psp_note=f"auth via {TEST_API_KEY}")
    assert TEST_API_KEY not in str(excinfo.value)


@pytest.mark.parametrize(
    "field", ["private_key", "seed_phrase", "mnemonic", "raw_token", "payment_token"]
)
def test_secret_named_fields_are_rejected(field: str) -> None:
    with pytest.raises(PaymentSecretRejectedError):
        _facet(**{field: "anything"})


def test_secret_nested_inside_an_extra_block_is_rejected() -> None:
    """`extra="allow"` must not become a hole in I-2."""
    with pytest.raises(PaymentSecretRejectedError):
        _facet(psp_metadata={"attempts": [{"card": TEST_PAN}]})


def test_rejection_names_the_field_path() -> None:
    with pytest.raises(PaymentSecretRejectedError) as excinfo:
        _facet(psp_metadata={"attempts": [{"card": TEST_PAN}]})
    assert excinfo.value.path == "psp_metadata.attempts[0].card"


def test_untrusted_json_cannot_smuggle_a_secret_past_model_validate() -> None:
    """I-2 must be structural: every construction path enforces it."""
    with pytest.raises(PaymentSecretRejectedError):
        SettlementFacet.model_validate({"protocol": "ap2", "pan": TEST_PAN})


def test_key_identifiers_are_not_mistaken_for_secrets() -> None:
    """ADR-0163 D2 requires a signer *key id* to be storable.

    The support bundle's broad `key|token|secret` key deny-list would reject
    this; rejecting the field the ADR mandates would be worse than the leak
    it prevents.
    """
    facet = _facet(signer_ref="network:visa-ic:key-2026")
    assert facet.model_dump()["signer_ref"] == "network:visa-ic:key-2026"


def test_digest_references_are_never_read_as_card_numbers() -> None:
    """A digit run inside a hex digest is not a PAN.

    Without delimiter-anchoring this fires on roughly 1 digest in 200, which
    would make the facet intermittently refuse to build.
    """
    hostile = "sha256:" + "4111111111111111" + "a" * 48
    reject_payment_secrets({"settlement_ref": hostile})


def test_a_plausible_reference_that_looks_like_an_iban_is_allowed() -> None:
    """Shape is not enough — mod-97 separates an account from a reference."""
    reject_payment_secrets({"scheme_ref": "EU2026SETTLEMENT0001"})


def test_ordinary_settlement_facet_is_clean() -> None:
    reject_payment_secrets(_facet(amount=Money(amount_minor=16240, currency="EUR")).model_dump())


# ── Money ─────────────────────────────────────────────────────────────────


def test_money_is_integer_minor_units() -> None:
    money = Money(amount_minor=16240, currency="EUR")
    assert money.amount_minor == 16240
    assert isinstance(money.amount_minor, int)


def test_money_refuses_a_fractional_amount() -> None:
    """Floats cannot represent 162.40, and P2 reconciliation compares amounts.

    An amount 0.01 out invents or hides an `over_authorized` discrepancy.
    """
    with pytest.raises(ValidationError):
        Money(amount_minor=162.40, currency="EUR")  # type: ignore[arg-type]


def test_money_refuses_a_negative_amount() -> None:
    """Reversals are their own positively-signed hops (NF-320), not negatives."""
    with pytest.raises(ValidationError):
        Money(amount_minor=-1, currency="EUR")


@pytest.mark.parametrize("code", ["eur", "EURO", "E", "12", ""])
def test_currency_must_be_an_iso_4217_alpha_3_code(code: str) -> None:
    with pytest.raises(ValidationError):
        Money(amount_minor=1, currency=code)


def test_zero_decimal_currency_needs_no_special_case() -> None:
    """500 is EUR 5.00 but JPY 500 — the code is what makes minor units mean."""
    assert Money(amount_minor=500, currency="JPY").amount_minor == 500


# ── I-3: additive-first / fail-open ───────────────────────────────────────


def test_run_that_moved_no_money_yields_no_facet() -> None:
    """Fail-open: absent settlement material is silence, not an exception."""
    assert build_facet(protocol="ap2") is None


def test_protocol_alone_is_not_evidence() -> None:
    """A protocol name binding nothing claims a payment with nothing to check."""
    assert build_facet(protocol="x402", protocol_version="1.0") is None


@pytest.mark.parametrize(
    "material",
    [
        {"mandate_ref": MANDATE_REF},
        {"settlement_ref": CONFIRMATION_REF},
        {"amount": Money(amount_minor=16240, currency="EUR")},
    ],
)
def test_any_single_piece_of_material_produces_a_facet(
    material: dict[str, object],
) -> None:
    assert build_facet(protocol="ap2", **material) is not None  # type: ignore[arg-type]


def test_capsule_without_settlement_material_is_untouched() -> None:
    """Byte-identical to a capsule captured before this feature existed."""
    capsule = {"run_id": "r", "facets": {"existing": {"a": 1}}}
    assert attach_facet(capsule, build_facet(protocol="ap2")) == capsule


def test_attach_does_not_mutate_the_input_capsule() -> None:
    capsule: dict[str, object] = {"run_id": "r"}
    attach_facet(capsule, _facet())
    assert capsule == {"run_id": "r"}


def test_attach_does_not_mutate_the_input_facets_block() -> None:
    facets: dict[str, object] = {"existing": {"a": 1}}
    capsule: dict[str, object] = {"run_id": "r", "facets": facets}
    attach_facet(capsule, _facet())
    assert facets == {"existing": {"a": 1}}


def test_attach_preserves_sibling_facets() -> None:
    capsule = {"run_id": "r", "facets": {"existing": {"a": 1}}}
    out = attach_facet(capsule, _facet())
    assert out["facets"]["existing"] == {"a": 1}
    assert "settlement" in out["facets"]


def test_attached_facet_carries_a_schema_version() -> None:
    out = attach_facet({"run_id": "r"}, _facet())
    assert out["facets"]["settlement"]["schema_version"]


def test_attached_facet_omits_unset_optional_fields() -> None:
    out = attach_facet({"run_id": "r"}, _facet(protocol_version=None, bound_root=None))
    settlement = out["facets"]["settlement"]
    assert "protocol_version" not in settlement
    assert "bound_root" not in settlement


def test_attach_over_an_existing_settlement_facet_replaces_it() -> None:
    capsule = {"run_id": "r", "facets": {"settlement": {"protocol": "x402"}}}
    out = attach_facet(capsule, _facet())
    assert out["facets"]["settlement"]["protocol"] == "ap2"


# ── I-1 / I-4: record-only, never determines ──────────────────────────────


def test_no_payload_reaches_the_capsule() -> None:
    """The mandate and confirmation are present only as digests."""
    dumped = _facet().model_dump_json()
    assert MANDATE not in dumped
    assert CONFIRMATION not in dumped
    assert MANDATE_REF in dumped
    assert CONFIRMATION_REF in dumped


def test_unknown_protocol_is_recorded_as_other_not_rejected() -> None:
    """An unlisted protocol is a fact to record, not an error (I-4)."""
    facet = build_facet(protocol="other", settlement_ref=CONFIRMATION_REF)
    assert facet is not None
    assert facet.protocol == "other"


def test_protocol_outside_the_bound_set_is_refused() -> None:
    """`other` exists precisely so nothing gets mislabelled as `ap2`."""
    with pytest.raises(ValidationError):
        _facet(protocol="paypal")


def test_module_exposes_no_payment_path_surface() -> None:
    """Never-in-the-payment-path is a property of the API, not just the docs.

    If a pay/settle/refund/capture entry point ever appears here, this fails —
    which is the point: I-1 should be structurally hard to violate.
    """
    import novafabric.settlement as settlement

    forbidden = {
        "pay",
        "settle",
        "refund",
        "capture",
        "authorize",
        "charge",
        "transfer",
        "hold",
        "release",
        "adjudicate",
    }
    assert forbidden.isdisjoint(
        {name.lower() for name in settlement.__all__}
    ), "settlement must not expose a payment-path entry point (ADR-0163 I-1)"
