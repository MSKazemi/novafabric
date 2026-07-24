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

"""ADR-0163 P2 — reconciliation, finality, non-repudiation (NF-312/313/314).

Organised by the three questions the slice answers, because each one has a
distinct way of going wrong:

- **NF-312** — did the charge match the mandate? The failure mode is a
  discrepancy that gets *resolved* (netted off, corrected, decided) instead of
  recorded.
- **NF-313** — did the money actually move? The failure mode is a not-yet-final
  state, or the absence of any state, rendering as ``settled``.
- **NF-314** — is the signature still anchored? The failure mode is a record
  that claims an intact binding it does not hold.

The test file is deliberately not named ``test_facet.py``: duplicate test
basenames across ``tests/`` directories previously broke collection outright.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from pydantic import ValidationError

from novafabric.settlement import (
    UNKNOWN_FINALITY,
    AuthorizedTerms,
    DiscrepancyLaunderedError,
    FinalityRecord,
    InvalidReferenceError,
    MandateReconciliation,
    Money,
    NonRepudiationBinding,
    ObservedSettlement,
    PaymentSecretRejectedError,
    SettlementFacet,
    UnconfirmedFinalityError,
    attach_facet,
    build_facet,
    build_non_repudiation,
    digest_artifact,
    finality_state,
    is_final,
    reconcile,
    verify_non_repudiation,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO_ROOT / "schemas" / "run-capsule.schema.json"
BASELINE = (
    REPO_ROOT / "tests" / "fixtures" / "model-provenance" / "valid-text-only-capsule.json"
)

MANDATE = '{"type":"PaymentMandate","max_amount":"150.00","currency":"EUR"}'
CONFIRMATION = '{"msg":"sese.025","status":"SETT","amount":"162.40"}'

MANDATE_REF = digest_artifact(MANDATE)
CONFIRMATION_REF = digest_artifact(CONFIRMATION)
ROOT_REF = digest_artifact("capsule-root")
OTHER_ROOT_REF = digest_artifact("some-other-capsule-root")

SIGNER = "network:visa-ic:key-2026"

#: EUR 150.00 authorized to `acme-merchant`, valid until 2026-07-20.
AUTHORIZED = AuthorizedTerms(
    max_amount=Money(amount_minor=15000, currency="EUR"),
    payee="acme-merchant",
    expiry="2026-07-20T00:00:00+00:00",
    scope=["travel", "accommodation"],
)


def _observed(**kw: Any) -> ObservedSettlement:
    base: dict[str, Any] = {
        "amount": Money(amount_minor=15000, currency="EUR"),
        "payee": "acme-merchant",
        "observed_at": "2026-07-13T10:02:11+00:00",
        "scope": "travel",
    }
    base.update(kw)
    return ObservedSettlement(**base)


def _finality(**kw: Any) -> FinalityRecord:
    base: dict[str, Any] = {
        "state": "settled",
        "finality_source": "network_confirmation",
        "confirmation_ref": CONFIRMATION_REF,
        "observed_at": "2026-07-13T10:02:11+00:00",
    }
    base.update(kw)
    return FinalityRecord(**base)


# ══ NF-312 — authorized ↔ observed reconciliation ═════════════════════════


def test_matching_charge_reconciles_cleanly() -> None:
    record = reconcile(AUTHORIZED, _observed())
    assert record.reconciled is True
    assert record.discrepancies == []
    assert set(record.compared) == {"currency", "amount", "payee", "expiry", "scope"}


def test_over_settlement_is_recorded_as_over_authorized() -> None:
    """The ADR's worked example: EUR 150 authorized, EUR 162.40 charged."""
    record = reconcile(
        AUTHORIZED, _observed(amount=Money(amount_minor=16240, currency="EUR"))
    )
    assert record.discrepancies == ["over_authorized"]
    assert record.reconciled is False


def test_under_settlement_is_not_a_discrepancy() -> None:
    """A mandate authorizes a *ceiling*, so a smaller charge is inside it.

    ADR-0163's discrepancy vocabulary has no `under_authorized` code, and
    inventing one would manufacture a finding `max_amount` does not support.
    An under-payment that genuinely is a problem is a commercial dispute
    (NF-317 assembles evidence for one); NovaFabric does not adjudicate it.
    """
    record = reconcile(
        AUTHORIZED, _observed(amount=Money(amount_minor=9900, currency="EUR"))
    )
    assert record.discrepancies == []
    assert record.reconciled is True


def test_charge_exactly_at_the_ceiling_is_clean() -> None:
    """The boundary: `over_authorized` is strictly greater-than."""
    record = reconcile(
        AUTHORIZED, _observed(amount=Money(amount_minor=15000, currency="EUR"))
    )
    assert record.discrepancies == []


def test_currency_mismatch_is_recorded() -> None:
    record = reconcile(
        AUTHORIZED, _observed(amount=Money(amount_minor=16240, currency="USD"))
    )
    assert "currency_mismatch" in record.discrepancies


def test_currency_mismatch_suppresses_the_amount_comparison() -> None:
    """USD 162.40 against EUR 150 is not `over_authorized` — it is incomparable.

    Calling it over-authorized needs an FX rate and a rate date, and choosing
    those would be NovaFabric adjudicating the charge (I-4).
    """
    record = reconcile(
        AUTHORIZED, _observed(amount=Money(amount_minor=16240, currency="USD"))
    )
    assert record.discrepancies == ["currency_mismatch"]
    assert "amount" not in record.compared


def test_currency_mismatch_suppresses_amount_even_when_far_below() -> None:
    """Suppression is about comparability, not about which way the gap runs."""
    record = reconcile(
        AUTHORIZED, _observed(amount=Money(amount_minor=1, currency="JPY"))
    )
    assert record.discrepancies == ["currency_mismatch"]


def test_payee_mismatch_is_recorded() -> None:
    record = reconcile(AUTHORIZED, _observed(payee="not-acme"))
    assert record.discrepancies == ["payee_mismatch"]


def test_charge_after_expiry_is_recorded() -> None:
    record = reconcile(AUTHORIZED, _observed(observed_at="2026-07-21T00:00:00+00:00"))
    assert record.discrepancies == ["expired_mandate"]


def test_charge_before_expiry_is_clean() -> None:
    record = reconcile(AUTHORIZED, _observed(observed_at="2026-07-19T23:59:59+00:00"))
    assert record.discrepancies == []


def test_out_of_scope_charge_is_recorded() -> None:
    record = reconcile(AUTHORIZED, _observed(scope="gambling"))
    assert record.discrepancies == ["out_of_scope"]


def test_several_discrepancies_are_all_recorded() -> None:
    """No discrepancy shadows another; the record is the full list."""
    record = reconcile(
        AUTHORIZED,
        _observed(
            amount=Money(amount_minor=99999, currency="EUR"),
            payee="not-acme",
            observed_at="2026-08-01T00:00:00+00:00",
            scope="gambling",
        ),
    )
    assert set(record.discrepancies) == {
        "over_authorized",
        "payee_mismatch",
        "expired_mandate",
        "out_of_scope",
    }
    assert record.reconciled is False


# ── The discrepancy is the evidence: recorded, never resolved ─────────────


def test_both_sides_are_preserved_verbatim() -> None:
    """Neither side is corrected, and no net figure is derived.

    An auditor needs the two numbers that disagree, not a third one this
    module invented from them.
    """
    observed = _observed(amount=Money(amount_minor=16240, currency="EUR"))
    record = reconcile(AUTHORIZED, observed)
    assert record.authorized.max_amount is not None
    assert record.observed.amount is not None
    assert record.authorized.max_amount.amount_minor == 15000
    assert record.observed.amount.amount_minor == 16240


def test_reconciliation_records_no_net_or_delta_field() -> None:
    """Netting the two amounts off would destroy the evidence (I-4)."""
    dumped = reconcile(
        AUTHORIZED, _observed(amount=Money(amount_minor=16240, currency="EUR"))
    ).model_dump()
    for forbidden in ("delta", "net", "difference", "adjusted", "corrected", "resolved"):
        assert not any(forbidden in key for key in dumped)


def test_amounts_stay_integer_minor_units() -> None:
    """A float reconciliation is not an audit input (P1's `Money` rule)."""
    record = reconcile(
        AUTHORIZED, _observed(amount=Money(amount_minor=16240, currency="EUR"))
    )
    assert record.observed.amount is not None
    assert isinstance(record.observed.amount.amount_minor, int)


def test_reconciliation_exposes_no_resolution_entry_point() -> None:
    """I-4 should be structurally hard to violate, not merely documented."""
    import novafabric.settlement as settlement

    forbidden = {"resolve", "net_off", "correct", "void", "dispute_outcome"}
    assert forbidden.isdisjoint({name.lower() for name in settlement.__all__})


# ── Absent is not false ───────────────────────────────────────────────────


def test_comparing_nothing_does_not_claim_a_match() -> None:
    """`reconciled` means "compared and matched", not "found no problem".

    An empty `discrepancies` list here says no discrepancy was *found*, not
    that none exists — nothing was read to find one with.
    """
    record = reconcile(AuthorizedTerms(), ObservedSettlement())
    assert record.compared == []
    assert record.discrepancies == []
    assert record.reconciled is False


@pytest.mark.parametrize(
    ("authorized", "observed", "absent_term"),
    [
        (AuthorizedTerms(payee="acme"), ObservedSettlement(), "payee"),
        (AuthorizedTerms(), ObservedSettlement(payee="acme"), "payee"),
        (
            AuthorizedTerms(max_amount=Money(amount_minor=1, currency="EUR")),
            ObservedSettlement(),
            "amount",
        ),
        (AuthorizedTerms(scope=["travel"]), ObservedSettlement(), "scope"),
        (AuthorizedTerms(), ObservedSettlement(scope="travel"), "scope"),
    ],
)
def test_a_term_present_on_only_one_side_is_not_compared(
    authorized: AuthorizedTerms, observed: ObservedSettlement, absent_term: str
) -> None:
    """A term that was not read is unknown — never "satisfied"."""
    record = reconcile(authorized, observed)
    assert absent_term not in record.compared
    assert record.discrepancies == []


def test_unparseable_expiry_yields_no_finding_and_does_not_raise() -> None:
    """A formatting problem must not become an `expired_mandate` finding."""
    record = reconcile(
        AuthorizedTerms(expiry="whenever"), _observed()
    )
    assert record.discrepancies == []
    assert "expiry" not in record.compared


def test_naive_and_aware_instants_are_treated_as_unreadable() -> None:
    """A mandate expiry written without an offset is a real producer habit."""
    record = reconcile(
        AuthorizedTerms(expiry="2026-07-20T00:00:00"),
        ObservedSettlement(observed_at="2026-07-21T00:00:00+00:00"),
    )
    assert record.discrepancies == []
    assert "expiry" not in record.compared


# ── The laundering guard (structural, not procedural) ─────────────────────


def test_a_record_cannot_show_a_discrepancy_and_claim_reconciled() -> None:
    with pytest.raises(DiscrepancyLaunderedError):
        MandateReconciliation(reconciled=True, discrepancies=["over_authorized"])


def test_untrusted_json_cannot_launder_a_discrepancy_into_a_match() -> None:
    """`model_validate` of a facet read off disk must not mint a clean verdict."""
    with pytest.raises(DiscrepancyLaunderedError):
        MandateReconciliation.model_validate(
            {
                "reconciled": True,
                "discrepancies": ["payee_mismatch"],
                "compared": ["payee"],
            }
        )


def test_a_record_cannot_claim_a_match_having_compared_nothing() -> None:
    with pytest.raises(DiscrepancyLaunderedError):
        MandateReconciliation(reconciled=True, compared=[])


def test_a_laundered_record_cannot_survive_a_round_trip() -> None:
    """`model_copy(update=…)` bypasses validation — pydantic semantics.

    So the guard that matters is the one on the way back *in*: a tampered
    record can be held in memory, but it can never be re-read from a capsule,
    which is the only path an auditor's verdict ever travels.
    """
    record = reconcile(
        AUTHORIZED, _observed(amount=Money(amount_minor=16240, currency="EUR"))
    )
    tampered = record.model_copy(update={"reconciled": True}).model_dump()
    with pytest.raises(DiscrepancyLaunderedError):
        MandateReconciliation.model_validate(tampered)


def test_laundering_error_is_not_a_validation_error() -> None:
    """A named type must survive; a laundered verdict is not a shape complaint."""
    assert not issubclass(DiscrepancyLaunderedError, ValueError)


# ══ NF-313 — settlement finality ══════════════════════════════════════════


@pytest.mark.parametrize(
    "state",
    ["initiated", "authorized", "captured", "settled", "pending", "failed", "reversed"],
)
def test_every_finality_state_round_trips(state: str) -> None:
    record = _finality(state=state)
    assert record.state == state


@pytest.mark.parametrize(
    "state",
    ["initiated", "authorized", "captured", "pending", "failed", "reversed"],
)
def test_only_settled_is_final(state: str) -> None:
    """Not-yet-final must never render as final."""
    facet = build_facet(protocol="ap2", finality=_finality(state=state))
    assert is_final(facet) is False
    assert finality_state(facet) == state


def test_settled_is_final() -> None:
    facet = build_facet(protocol="ap2", finality=_finality(state="settled"))
    assert is_final(facet) is True


def test_captured_is_not_settled() -> None:
    """Money taken by the merchant is not money settled by the acquirer.

    ADR-0163's enum omits `captured`; this slice adds it because folding the
    state into `authorized` understates it and folding it into `settled`
    commits the exact error NF-313 exists to prevent.
    """
    facet = build_facet(protocol="ap2", finality=_finality(state="captured"))
    assert is_final(facet) is False
    assert finality_state(facet) == "captured"


def test_authorized_is_not_captured_or_settled() -> None:
    facet = build_facet(protocol="ap2", finality=_finality(state="authorized"))
    assert finality_state(facet) == "authorized"
    assert is_final(facet) is False


def test_reversed_is_not_final() -> None:
    """Reversed money moved and then moved back; it is not settled."""
    facet = build_facet(protocol="ap2", finality=_finality(state="reversed"))
    assert is_final(facet) is False


def test_absent_finality_record_is_unknown_never_settled() -> None:
    """The auditor's question is 'did money move'; silence is not a 'yes'."""
    facet = build_facet(protocol="ap2", settlement_ref=CONFIRMATION_REF)
    assert facet is not None
    assert facet.finality is None
    assert finality_state(facet) == UNKNOWN_FINALITY
    assert is_final(facet) is False


def test_absent_facet_entirely_is_unknown_never_settled() -> None:
    assert finality_state(None) == UNKNOWN_FINALITY
    assert is_final(None) is False


def test_unknown_is_not_reported_as_a_failure_either() -> None:
    """`unknown` must not be laundered into `failed` any more than `settled`."""
    assert UNKNOWN_FINALITY not in {"settled", "failed", "reversed", "pending"}


@pytest.mark.parametrize(
    "source", ["network_confirmation", "onchain_confirmation"]
)
def test_a_corroborated_source_requires_the_confirmation_reference(
    source: str,
) -> None:
    """Claiming corroboration while holding none is refused, not downgraded."""
    with pytest.raises(UnconfirmedFinalityError):
        _finality(finality_source=source, confirmation_ref=None)


def test_a_declared_state_needs_no_confirmation_reference() -> None:
    """`declared` is the honest home for an uncorroborated producer claim."""
    record = _finality(finality_source="declared", confirmation_ref=None)
    assert record.finality_source == "declared"
    assert record.confirmation_ref is None


def test_a_declared_settled_state_is_still_marked_declared() -> None:
    """The source stays visible so a claim is never read as a confirmation."""
    facet = build_facet(
        protocol="ap2",
        finality=_finality(
            state="settled", finality_source="declared", confirmation_ref=None
        ),
    )
    assert facet is not None
    assert facet.finality is not None
    assert facet.finality.finality_source == "declared"


def test_confirmation_reference_must_be_a_digest() -> None:
    with pytest.raises(InvalidReferenceError):
        _finality(confirmation_ref="https://psp.example/confirmations/42")


def test_finality_requires_an_observed_at() -> None:
    """A state with no time cannot be ordered against a later reversal."""
    with pytest.raises(ValidationError):
        FinalityRecord(
            state="settled",
            finality_source="declared",
        )  # type: ignore[call-arg]


def test_unknown_finality_state_is_refused() -> None:
    with pytest.raises(ValidationError):
        _finality(state="cleared")


# ══ NF-314 — non-repudiation binding ══════════════════════════════════════


def test_a_complete_unchecked_anchor_is_recorded_intact() -> None:
    """An unchecked anchor is not a finding; inventing one reports nothing real."""
    binding = build_non_repudiation(
        sig_scheme="iso20022_cms",
        signed_digest=CONFIRMATION_REF,
        signer_ref=SIGNER,
        bound_root=ROOT_REF,
    )
    assert binding.non_repudiation_broken is False


def test_re_verification_against_the_artifact_keeps_the_anchor_intact() -> None:
    binding = build_non_repudiation(
        sig_scheme="iso20022_cms",
        signed_digest=CONFIRMATION_REF,
        signer_ref=SIGNER,
        bound_root=ROOT_REF,
        resolver=lambda _ref: CONFIRMATION,
        capsule_root=ROOT_REF,
    )
    assert binding.non_repudiation_broken is False
    assert verify_non_repudiation(binding, artifact=CONFIRMATION, capsule_root=ROOT_REF)


def test_re_verification_failure_breaks_the_anchor() -> None:
    """The ref names something other than the artifact behind it."""
    binding = build_non_repudiation(
        sig_scheme="iso20022_cms",
        signed_digest=CONFIRMATION_REF,
        signer_ref=SIGNER,
        bound_root=ROOT_REF,
        resolver=lambda _ref: CONFIRMATION + " tampered",
    )
    assert binding.non_repudiation_broken is True


def test_an_unresolvable_artifact_breaks_the_anchor() -> None:
    binding = build_non_repudiation(
        sig_scheme="detached_jws",
        signed_digest=CONFIRMATION_REF,
        signer_ref=SIGNER,
        bound_root=ROOT_REF,
        resolver=lambda _ref: None,
    )
    assert binding.non_repudiation_broken is True


def test_a_raising_resolver_breaks_the_anchor_without_failing_the_capsule() -> None:
    """Fail-open (I-3): a lookup error must not cost the run its capsule."""

    def _explode(_ref: str) -> str:
        raise RuntimeError("evidence store offline")

    binding = build_non_repudiation(
        sig_scheme="w3c_vc",
        signed_digest=CONFIRMATION_REF,
        signer_ref=SIGNER,
        bound_root=ROOT_REF,
        resolver=_explode,
    )
    assert binding.non_repudiation_broken is True


def test_an_anchor_naming_another_capsule_root_is_broken() -> None:
    binding = build_non_repudiation(
        sig_scheme="iso20022_cms",
        signed_digest=CONFIRMATION_REF,
        signer_ref=SIGNER,
        bound_root=OTHER_ROOT_REF,
        capsule_root=ROOT_REF,
    )
    assert binding.non_repudiation_broken is True


@pytest.mark.parametrize(
    "missing", ["signed_digest", "signer_ref", "bound_root"]
)
def test_an_incomplete_anchor_is_broken(missing: str) -> None:
    """Missing any one of the three references and there is no anchor."""
    kwargs: dict[str, Any] = {
        "sig_scheme": "iso20022_cms",
        "signed_digest": CONFIRMATION_REF,
        "signer_ref": SIGNER,
        "bound_root": ROOT_REF,
        "non_repudiation_broken": False,
    }
    kwargs[missing] = None
    assert NonRepudiationBinding(**kwargs).non_repudiation_broken is True


def test_a_blank_signer_reference_is_broken() -> None:
    """Nobody asserting the signature is the same as no anchor at all."""
    binding = NonRepudiationBinding(
        sig_scheme="iso20022_cms",
        signed_digest=CONFIRMATION_REF,
        signer_ref="   ",
        bound_root=ROOT_REF,
        non_repudiation_broken=False,
    )
    assert binding.non_repudiation_broken is True


def test_untrusted_json_cannot_construct_an_intact_looking_binding() -> None:
    """The core NF-314 trap.

    A forged `non_repudiation_broken: false` on a record with no `bound_root`
    would present an unanchored signature as a tamper-evident one. Enforced in
    the model validator, so `model_validate` of untrusted JSON cannot mint it.
    """
    binding = NonRepudiationBinding.model_validate(
        {
            "sig_scheme": "iso20022_cms",
            "signed_digest": CONFIRMATION_REF,
            "signer_ref": SIGNER,
            "non_repudiation_broken": False,
        }
    )
    assert binding.non_repudiation_broken is True


def test_untrusted_json_cannot_smuggle_an_intact_binding_into_a_facet() -> None:
    """Same trap, one level up: a whole facet read off disk."""
    facet = SettlementFacet.model_validate(
        {
            "protocol": "ap2",
            "settlement_ref": CONFIRMATION_REF,
            "non_repudiation": {
                "sig_scheme": "w3c_vc",
                "signed_digest": CONFIRMATION_REF,
                "non_repudiation_broken": False,
            },
        }
    )
    assert facet.non_repudiation is not None
    assert facet.non_repudiation.non_repudiation_broken is True


def test_a_transplanted_anchor_from_another_capsule_is_broken() -> None:
    """Lift an intact binding out of a real capsule, paste it into a forged one.

    Every field-level check still passes; only the cross-field comparison
    against the facet's own `bound_root` catches it.
    """
    facet = SettlementFacet.model_validate(
        {
            "protocol": "ap2",
            "settlement_ref": CONFIRMATION_REF,
            "bound_root": ROOT_REF,
            "non_repudiation": {
                "sig_scheme": "iso20022_cms",
                "signed_digest": CONFIRMATION_REF,
                "signer_ref": SIGNER,
                "bound_root": OTHER_ROOT_REF,
                "non_repudiation_broken": False,
            },
        }
    )
    assert facet.non_repudiation is not None
    assert facet.non_repudiation.non_repudiation_broken is True


def test_a_transplanted_anchor_is_recorded_not_refused() -> None:
    """Raising would delete the evidence that someone tried it."""
    facet = build_facet(
        protocol="ap2",
        bound_root=ROOT_REF,
        non_repudiation=build_non_repudiation(
            sig_scheme="iso20022_cms",
            signed_digest=CONFIRMATION_REF,
            signer_ref=SIGNER,
            bound_root=OTHER_ROOT_REF,
        ),
    )
    assert facet is not None
    dumped = attach_facet({"run_id": "r"}, facet)["facets"]["settlement"]
    assert dumped["non_repudiation"]["non_repudiation_broken"] is True


def test_the_broken_flag_survives_serialisation() -> None:
    """A `false` bool must not be optimised out of the sealed record."""
    facet = build_facet(
        protocol="ap2",
        bound_root=ROOT_REF,
        non_repudiation=build_non_repudiation(
            sig_scheme="iso20022_cms",
            signed_digest=CONFIRMATION_REF,
            signer_ref=SIGNER,
            bound_root=ROOT_REF,
        ),
    )
    assert facet is not None
    dumped = attach_facet({"run_id": "r"}, facet)["facets"]["settlement"]
    assert dumped["non_repudiation"]["non_repudiation_broken"] is False


def test_signed_digest_must_be_a_digest_not_the_artifact() -> None:
    with pytest.raises(InvalidReferenceError):
        NonRepudiationBinding(sig_scheme="iso20022_cms", signed_digest=CONFIRMATION)


def test_signer_key_identifier_is_not_mistaken_for_a_secret() -> None:
    """ADR-0163 D2 requires a key *identifier* to be storable (P1's rule)."""
    binding = NonRepudiationBinding(
        sig_scheme="iso20022_cms",
        signed_digest=CONFIRMATION_REF,
        signer_ref=SIGNER,
        bound_root=ROOT_REF,
    )
    assert binding.signer_ref == SIGNER


# ── verify_non_repudiation ────────────────────────────────────────────────


def test_verification_of_a_broken_binding_is_false() -> None:
    binding = NonRepudiationBinding(sig_scheme="w3c_vc")
    assert verify_non_repudiation(binding) is False


def test_verification_against_the_wrong_artifact_is_false() -> None:
    binding = build_non_repudiation(
        sig_scheme="iso20022_cms",
        signed_digest=CONFIRMATION_REF,
        signer_ref=SIGNER,
        bound_root=ROOT_REF,
    )
    assert verify_non_repudiation(binding, artifact=MANDATE) is False


def test_verification_against_the_wrong_root_is_false() -> None:
    binding = build_non_repudiation(
        sig_scheme="iso20022_cms",
        signed_digest=CONFIRMATION_REF,
        signer_ref=SIGNER,
        bound_root=ROOT_REF,
    )
    assert verify_non_repudiation(binding, capsule_root=OTHER_ROOT_REF) is False


def test_verification_with_nothing_to_check_reports_the_recorded_state() -> None:
    """"Not checked" must not become "checked and intact"."""
    intact = build_non_repudiation(
        sig_scheme="iso20022_cms",
        signed_digest=CONFIRMATION_REF,
        signer_ref=SIGNER,
        bound_root=ROOT_REF,
    )
    broken = NonRepudiationBinding(sig_scheme="iso20022_cms")
    assert verify_non_repudiation(intact) is True
    assert verify_non_repudiation(broken) is False


# ══ Facet integration — additive-first, secret-free ═══════════════════════


@pytest.mark.parametrize(
    "material",
    [
        {"mandate_reconciliation": reconcile(AUTHORIZED, _observed())},
        {"finality": _finality()},
        {
            "non_repudiation": build_non_repudiation(
                sig_scheme="iso20022_cms",
                signed_digest=CONFIRMATION_REF,
                signer_ref=SIGNER,
                bound_root=ROOT_REF,
            )
        },
    ],
)
def test_any_p2_block_alone_produces_a_facet(material: dict[str, Any]) -> None:
    assert build_facet(protocol="ap2", **material) is not None


def test_a_run_with_no_settlement_material_still_yields_no_facet() -> None:
    """P2 must not turn a fail-open no-op into a facet (I-3)."""
    assert build_facet(protocol="ap2") is None


def test_a_capsule_with_no_settlement_material_is_unmutated() -> None:
    capsule = {"run_id": "r", "facets": {"existing": {"a": 1}}}
    assert attach_facet(capsule, build_facet(protocol="ap2")) == capsule


def test_a_p1_facet_carries_no_p2_keys() -> None:
    """Absent is not false: a P1 facet must not sprout empty P2 blocks."""
    facet = build_facet(protocol="ap2", settlement_ref=CONFIRMATION_REF)
    dumped = attach_facet({"run_id": "r"}, facet)["facets"]["settlement"]
    assert "mandate_reconciliation" not in dumped
    assert "finality" not in dumped
    assert "non_repudiation" not in dumped


def test_a_payment_secret_inside_a_p2_block_is_rejected() -> None:
    """P1's detector covers the new blocks; there is no second detector."""
    with pytest.raises(PaymentSecretRejectedError):
        build_facet(
            protocol="ap2",
            finality=_finality(pan_seen="4111111111111111"),
        )


def test_a_payment_secret_inside_reconciliation_extras_is_rejected() -> None:
    with pytest.raises(PaymentSecretRejectedError):
        SettlementFacet.model_validate(
            {
                "protocol": "ap2",
                "mandate_reconciliation": {
                    "psp_metadata": {"card": "4111111111111111"}
                },
            }
        )


def test_no_artifact_bytes_reach_the_capsule_through_the_p2_blocks() -> None:
    facet = build_facet(
        protocol="ap2",
        bound_root=ROOT_REF,
        mandate_reconciliation=reconcile(AUTHORIZED, _observed()),
        finality=_finality(),
        non_repudiation=build_non_repudiation(
            sig_scheme="iso20022_cms",
            signed_digest=CONFIRMATION_REF,
            signer_ref=SIGNER,
            bound_root=ROOT_REF,
        ),
    )
    assert facet is not None
    dumped = facet.model_dump_json()
    assert CONFIRMATION not in dumped
    assert MANDATE not in dumped


# ══ Real-schema validation ════════════════════════════════════════════════


@pytest.fixture(scope="module")
def schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text())


@pytest.fixture
def capsule() -> dict[str, Any]:
    return json.loads(BASELINE.read_text())


def test_a_full_p2_facet_validates_against_the_real_schema(
    schema: dict[str, Any], capsule: dict[str, Any]
) -> None:
    """Validated against `run-capsule.schema.json`, not a hand-written dict.

    ADR-0196 records five facet slices that passed their own gates while
    writing a capsule the real schema rejected, because every one of their
    tests operated on plain dicts.
    """
    facet = build_facet(
        protocol="ap2",
        protocol_version="0.2",
        mandate_ref=MANDATE_REF,
        settlement_ref=CONFIRMATION_REF,
        bound_root=ROOT_REF,
        amount=Money(amount_minor=16240, currency="EUR"),
        mandate_reconciliation=reconcile(
            AUTHORIZED, _observed(amount=Money(amount_minor=16240, currency="EUR"))
        ),
        finality=_finality(),
        non_repudiation=build_non_repudiation(
            sig_scheme="iso20022_cms",
            signed_digest=CONFIRMATION_REF,
            signer_ref=SIGNER,
            bound_root=ROOT_REF,
        ),
    )
    jsonschema.validate(attach_facet(capsule, facet), schema)


def test_a_capsule_with_no_facet_still_validates(
    schema: dict[str, Any], capsule: dict[str, Any]
) -> None:
    out = attach_facet(capsule, build_facet(protocol="ap2"))
    assert out == capsule
    jsonschema.validate(out, schema)
