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

"""ADR-0170 P1 — actuarial facet + incident-loss bind (NF-381/382).

Tests are organised by the ADR's invariants, because those are what a
reviewer needs to be convinced of: I-1 record-only, I-2 digests-and-counts
only, I-3 additive-first / fail-open, I-4 declared-is-not-measured — plus the
three golden fixtures P1 names, validated against the *real* capsule schema.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from pydantic import ValidationError

from novafabric.risk_transfer import (
    ActuarialBlock,
    FloatAmountRejectedError,
    IncidentLoss,
    InvalidReferenceError,
    LossFeature,
    LossItem,
    MissingDeclaredByError,
    MissingIncidentBundleError,
    Money,
    PaymentSecretRejectedError,
    attach_facet,
    build_actuarial,
    build_facet,
    build_incident_loss,
    digest_artifact,
    extract_loss_features,
    is_measured,
    verify_ref_binding,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO_ROOT / "schemas" / "run-capsule.schema.json"
BASELINE = (
    REPO_ROOT / "tests" / "fixtures" / "model-provenance" / "valid-text-only-capsule.json"
)

#: Stand-in for the NF-233 DFIR bundle. Never inlined into the facet — only
#: its digest is, which is the whole point of D2.
DFIR_BUNDLE = '{"kind":"dfir-bundle","incident":"INC-42","timeline":["…"]}'
BUNDLE_REF = digest_artifact(DFIR_BUNDLE)
ROOT_REF = digest_artifact("capsule-root")

#: A Luhn-valid test PAN (the ISO/IEC 7812 Visa test number; not a real card).
TEST_PAN = "4111111111111111"


@pytest.fixture(scope="module")
def schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text())


@pytest.fixture
def capsule() -> dict[str, Any]:
    return json.loads(BASELINE.read_text())


def _item(**kw: object) -> LossItem:
    base: dict[str, object] = {
        "category": "remediation",
        "amount": Money(amount_minor=4_200_000, currency="USD"),
        "basis": "vendor_invoice_digest",
        "declared_by": "deployer",
        "source": "declared",
    }
    base.update(kw)
    return LossItem(**base)  # type: ignore[arg-type]


def _resolves(content: str) -> Callable[[str], str]:
    return lambda _ref: content


# ── Golden fixtures (the three P1 names) ──────────────────────────────────


def test_golden_feature_free_capsule_stays_valid_and_byte_identical(
    schema: dict[str, Any], capsule: dict[str, Any]
) -> None:
    """Golden 1: a run with no risk-transfer material is untouched.

    Not merely "still validates": the returned object must be the capsule
    itself, because I-3 promises a capsule captured after this feature is
    identical to one captured before it.
    """
    facet = build_facet(actuarial=build_actuarial(loss_features=[]))
    out = attach_facet(capsule, facet)

    assert facet is None
    assert out == capsule
    assert "facets" not in out
    jsonschema.validate(out, schema)


def test_golden_valid_facet_validates_against_the_real_schema(
    schema: dict[str, Any], capsule: dict[str, Any]
) -> None:
    """Golden 2: a populated facet on a real capsule, against the real schema.

    Validated against ``run-capsule.schema.json`` rather than a hand-written
    expectation: five earlier facet slices shipped writing a ``facets`` key
    the schema rejected, precisely because their tests only ever looked at
    dicts of their own making (ADR-0196 D4).
    """
    facet = build_facet(
        actuarial=build_actuarial(
            loss_features=[
                LossFeature(feature="guardrail_trip", kind="guardrail_trip", count=3),
                LossFeature(
                    feature="run_failure",
                    kind="failure_mode",
                    count=1,
                    value_digest=digest_artifact("ToolInvocationError"),
                ),
            ],
            capsule_refs=[ROOT_REF],
            bound_root=ROOT_REF,
        ),
        incident_loss=build_incident_loss(
            incident_bundle_ref=BUNDLE_REF,
            loss_items=[_item(), _item(category="downtime", source="observed")],
            resolver=_resolves(DFIR_BUNDLE),
            quantified_at="2026-07-20T00:00:00Z",
            bound_root=ROOT_REF,
        ),
    )
    out = attach_facet(capsule, facet)

    jsonschema.validate(out, schema)
    block = out["facets"]["risk_transfer"]
    assert block["incident_loss"]["unbound"] is False
    assert block["incident_loss"]["incident_bundle_ref"] == BUNDLE_REF
    assert len(block["actuarial"]["loss_features"]) == 2


def test_golden_unresolvable_bundle_ref_is_recorded_as_unbound(
    schema: dict[str, Any], capsule: dict[str, Any]
) -> None:
    """Golden 3: an unresolvable ref is `unbound`, not an error and not a drop.

    An incident loss with no producible bundle behind it is exactly what an
    underwriter needs to see; dropping the ref, or leaving it looking bound,
    would hide the one fact that changes how the figure should be read.
    """
    record = build_incident_loss(
        incident_bundle_ref=BUNDLE_REF,
        loss_items=[_item()],
        resolver=lambda _ref: None,
    )
    out = attach_facet(capsule, build_facet(incident_loss=record))

    assert record.unbound is True
    assert record.incident_bundle_ref == BUNDLE_REF
    dumped = out["facets"]["risk_transfer"]["incident_loss"]
    assert dumped["unbound"] is True
    assert dumped["loss_items"]  # the declared loss survives; only its binding failed
    jsonschema.validate(out, schema)


# ── I-1: record-only ──────────────────────────────────────────────────────


def test_module_exposes_no_underwriting_surface() -> None:
    """Record-only is a property of the API, not just of the docs.

    If a price/rate/underwrite/score entry point ever appears here, this
    fails — which is the point: I-1 should be structurally hard to violate.
    """
    import novafabric.risk_transfer as risk_transfer

    forbidden = {
        "underwrite",
        "price",
        "rate",
        "premium",
        "adjudicate",
        "settle",
        "payout",
        "score",
        "total",
        "assign_liability",
    }
    names = {name.lower() for name in risk_transfer.__all__}
    assert forbidden.isdisjoint(names), (
        "risk_transfer must not expose an underwriting or adjudication entry "
        "point (ADR-0170 I-1)"
    )
    substrings = ("underwrit", "premium", "adjudicat", "payout", "risk_score")
    assert not [n for n in names if any(s in n for s in substrings)]


def test_no_loss_total_is_computed(capsule: dict[str, Any]) -> None:
    """Totalling a loss schedule is the adjuster's function, not NovaFabric's.

    Two items go in; two items come out, each with its own amount. Nothing in
    the record aggregates them — a total NovaFabric wrote would be read as a
    NovaFabric-determined loss.
    """
    record = build_incident_loss(
        incident_bundle_ref=BUNDLE_REF,
        loss_items=[_item(), _item(category="downtime")],
    )
    dumped = attach_facet(capsule, build_facet(incident_loss=record))["facets"][
        "risk_transfer"
    ]["incident_loss"]

    assert len(dumped["loss_items"]) == 2
    assert not {"total", "total_minor", "sum", "loss_total"} & set(dumped)


# ── I-2: digests and counts only ──────────────────────────────────────────


def test_failure_mode_is_a_digest_and_the_narrative_never_enters() -> None:
    """The error *type* is digested; the message is never read at all.

    `error.message` is sanitized for secrets but it is still the story of what
    happened — the narrative I-2 keeps out of an actuarial record.
    """
    message = "customer ledger for acct Jane Doe was overwritten"
    features = extract_loss_features(
        {
            "status": "failure",
            "error": {"type": "ToolInvocationError", "message": message},
        }
    )
    dumped = json.dumps([f.model_dump() for f in features])

    assert features[0].value_digest == digest_artifact("ToolInvocationError")
    assert message not in dumped
    assert "ToolInvocationError" not in dumped


def test_value_digest_must_be_a_digest() -> None:
    with pytest.raises(InvalidReferenceError):
        LossFeature(
            feature="run_failure", kind="failure_mode", value_digest="ToolInvocationError"
        )


def test_bundle_ref_must_be_a_digest() -> None:
    """A URI names a place the bundle was; only a digest binds its bytes."""
    with pytest.raises(InvalidReferenceError):
        build_incident_loss(incident_bundle_ref="file:///incidents/INC-42.json")


def test_capsule_refs_must_be_digests() -> None:
    with pytest.raises(InvalidReferenceError):
        ActuarialBlock(capsule_refs=["run-01HXAY7M5JZ8R7K4P9DPBYK2WX"])


def test_account_number_in_an_extra_field_is_refused() -> None:
    """`extra="allow"` is mandated, so the open part of the shape needs a guard.

    A claimant's card number reaching a loss record is an integration bug that
    means the caller is holding one elsewhere too; storing a redaction marker
    would tell it everything is fine.
    """
    with pytest.raises(PaymentSecretRejectedError):
        build_facet(
            incident_loss=IncidentLoss(
                incident_bundle_ref=BUNDLE_REF,
                loss_items=[_item(basis=f"chargeback on {TEST_PAN}")],
            )
        )


def test_rate_features_record_two_integers_not_a_float() -> None:
    """A rate is the consumer's arithmetic; the counts are the fact.

    Storing 0.1666… would be lossy, and dividing is one step towards the
    rating this module does not do.
    """
    feature = LossFeature(
        feature="tool_error", kind="tool_error_rate", count=2, denominator=12
    )
    dumped = feature.model_dump(exclude_none=True)

    assert (dumped["count"], dumped["denominator"]) == (2, 12)
    assert all(isinstance(v, int) for v in (dumped["count"], dumped["denominator"]))
    assert "rate" not in dumped


def test_feature_with_nothing_measured_is_refused() -> None:
    from novafabric.risk_transfer import UnquantifiedFeatureError

    with pytest.raises(UnquantifiedFeatureError):
        LossFeature(feature="plan_deviation", kind="deviation")


# ── Absent is not false ───────────────────────────────────────────────────


def test_missing_safety_evidence_yields_no_feature_rather_than_zero() -> None:
    """No safety facet means *not recorded*, never "nothing tripped".

    Emitting `count: 0` here would let a run with no guardrail instrumentation
    read to an underwriter exactly like a run that was instrumented and stayed
    clean.
    """
    assert extract_loss_features({"run_id": "r", "status": "success"}) == []


def test_present_but_empty_safety_evidence_yields_a_recorded_zero() -> None:
    features = extract_loss_features({"facets": {"safety": {"decisions": []}}})
    assert [(f.kind, f.count) for f in features] == [("guardrail_trip", 0)]


def test_guardrail_trips_count_blocks_and_rewrites_only() -> None:
    features = extract_loss_features(
        {
            "facets": {
                "safety": {
                    "decisions": [
                        {"disposition": "block"},
                        {"disposition": "rewrite"},
                        {"disposition": "allow"},
                    ]
                }
            }
        }
    )
    assert features[0].count == 2


def test_successful_run_records_no_failure_mode() -> None:
    assert extract_loss_features({"status": "success", "error": None}) == []


# ── I-3: additive-first / fail-open ───────────────────────────────────────


def test_attach_does_not_mutate_the_input_capsule() -> None:
    capsule: dict[str, object] = {"run_id": "r"}
    attach_facet(capsule, build_facet(incident_loss=_bound_record()))
    assert capsule == {"run_id": "r"}


def test_attach_preserves_sibling_facets() -> None:
    capsule = {"run_id": "r", "facets": {"safety": {"schema_version": "0.1.0"}}}
    out = attach_facet(capsule, build_facet(incident_loss=_bound_record()))
    assert out["facets"]["safety"] == {"schema_version": "0.1.0"}
    assert "risk_transfer" in out["facets"]


def test_facet_carries_a_schema_version() -> None:
    out = attach_facet({"run_id": "r"}, build_facet(incident_loss=_bound_record()))
    assert out["facets"]["risk_transfer"]["schema_version"]


def test_actuarial_block_with_no_features_is_not_emitted() -> None:
    """A block that names capsules but carries no feature asserts a collection
    process this module cannot vouch for on the caller's behalf."""
    assert build_actuarial(capsule_refs=[ROOT_REF], bound_root=ROOT_REF) is None


def test_extraction_never_raises_on_an_odd_capsule() -> None:
    """Fail-open: capture must not be blocked by a shape surprise."""
    assert extract_loss_features({"facets": "not-a-mapping", "error": 7}) == []
    assert extract_loss_features({"facets": {"safety": {"decisions": "nope"}}})[0].count == 0


def test_resolver_failure_is_recorded_as_unbound_not_raised() -> None:
    """A resolver that raised told us about itself, not about the bundle."""

    def _boom(_ref: str) -> str:
        raise OSError("evidence store offline")

    assert build_incident_loss(
        incident_bundle_ref=BUNDLE_REF, resolver=_boom
    ).unbound is True


def test_missing_bundle_ref_is_a_caller_bug_not_a_fail_open_case() -> None:
    """D2 forbids emitting the record without a bound bundle.

    Note the asymmetry with `unbound`: an *unresolvable* ref is a fact worth
    recording; *no ref at all* leaves nothing to record.
    """
    with pytest.raises(MissingIncidentBundleError):
        build_incident_loss(incident_bundle_ref=None, loss_items=[_item()])


# ── Binding ───────────────────────────────────────────────────────────────


def test_digest_accepts_bytes_and_str_identically() -> None:
    assert digest_artifact(DFIR_BUNDLE) == digest_artifact(DFIR_BUNDLE.encode())


def test_binding_verifies_against_the_bound_bundle() -> None:
    assert verify_ref_binding(BUNDLE_REF, DFIR_BUNDLE) is True
    assert verify_ref_binding(BUNDLE_REF, DFIR_BUNDLE + " ") is False


def test_unbound_reference_does_not_verify() -> None:
    assert verify_ref_binding(None, DFIR_BUNDLE) is False


def test_ref_resolving_to_different_bytes_is_unbound() -> None:
    """A lookup that succeeded is not a binding that holds.

    Calling this "bound" because something came back is the more dangerous of
    the two failure modes: the record would claim evidence for a loss that the
    named bytes do not support.
    """
    record = build_incident_loss(
        incident_bundle_ref=BUNDLE_REF,
        resolver=_resolves('{"kind":"dfir-bundle","incident":"INC-99"}'),
    )
    assert record.unbound is True


def test_unchecked_ref_is_not_marked_unbound() -> None:
    """With no resolver there was no check; inventing a finding would be a lie."""
    assert build_incident_loss(incident_bundle_ref=BUNDLE_REF).unbound is False


# ── I-4: declared is not measured ─────────────────────────────────────────


def test_loss_item_without_a_declarant_is_refused() -> None:
    with pytest.raises(ValidationError):
        LossItem(  # type: ignore[call-arg]
            category="remediation",
            amount=Money(amount_minor=1, currency="USD"),
            source="declared",
        )


def test_blank_declarant_is_refused_with_a_named_error() -> None:
    with pytest.raises(MissingDeclaredByError):
        _item(declared_by="   ")


def test_declared_figure_is_distinguishable_from_a_measured_one(
    capsule: dict[str, Any],
) -> None:
    """The point of the facet: an assertion must never read as a measurement."""
    declared = _item(declared_by="deployer", source="declared")
    estimated = _item(declared_by="loss_adjuster", source="estimated_by_third_party")
    measured = _item(declared_by="novafabric-capture", source="observed")

    assert is_measured(measured) is True
    assert is_measured(declared) is False
    # An estimate by a third party is still an assertion — it differs from a
    # declaration only in who made it, not in its evidential weight.
    assert is_measured(estimated) is False

    record = build_incident_loss(
        incident_bundle_ref=BUNDLE_REF, loss_items=[declared, estimated, measured]
    )
    dumped = attach_facet(capsule, build_facet(incident_loss=record))["facets"][
        "risk_transfer"
    ]["incident_loss"]["loss_items"]

    assert [i["source"] for i in dumped] == [
        "declared",
        "estimated_by_third_party",
        "observed",
    ]
    assert [i["declared_by"] for i in dumped] == [
        "deployer",
        "loss_adjuster",
        "novafabric-capture",
    ]


def test_provenance_survives_the_exclude_none_dump(capsule: dict[str, Any]) -> None:
    """`exclude_none` prunes unset optionals; it must never prune provenance."""
    record = build_incident_loss(
        incident_bundle_ref=BUNDLE_REF, loss_items=[_item(basis=None)]
    )
    item = attach_facet(capsule, build_facet(incident_loss=record))["facets"][
        "risk_transfer"
    ]["incident_loss"]["loss_items"][0]

    assert "basis" not in item
    assert item["declared_by"] and item["source"]


# ── Money ─────────────────────────────────────────────────────────────────


def test_amount_is_integer_minor_units_with_a_currency() -> None:
    money = Money(amount_minor=4_200_000, currency="USD")
    assert (money.amount_minor, money.currency) == (4_200_000, "USD")


@pytest.mark.parametrize("bad", [42000.0, 420.55, "42000"])
def test_non_integer_amounts_are_refused(bad: object) -> None:
    """A schedule of floats does not sum to the same number twice."""
    with pytest.raises(FloatAmountRejectedError):
        Money(amount_minor=bad, currency="USD")  # type: ignore[arg-type]


def test_negative_amount_is_refused() -> None:
    """A recovery is its own positively-signed item, not a negative loss."""
    with pytest.raises(ValidationError):
        Money(amount_minor=-1, currency="USD")


@pytest.mark.parametrize("bad", ["usd", "DOLLAR", "US"])
def test_currency_must_be_an_iso_4217_alpha_3_code(bad: str) -> None:
    with pytest.raises(ValidationError):
        Money(amount_minor=1, currency=bad)


def _bound_record() -> IncidentLoss:
    return build_incident_loss(
        incident_bundle_ref=BUNDLE_REF,
        loss_items=[_item()],
        resolver=_resolves(DFIR_BUNDLE),
    )
