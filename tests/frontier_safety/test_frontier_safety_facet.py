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

"""ADR-0167 P1 — frontier-safety facet (NF-351, NF-353).

Tests are organised by the ADR's invariants, because those are what a reviewer
needs to be convinced of: I-1 record-only, I-2 additive-first/fail-open,
I-3 never a computed verdict, I-4 absent is not false, I-5 no payloads.

The I-3 section is the heart of the slice. If NovaFabric could author a
frontier-safety verdict it would appear to certify a model as safe — the most
consequential error available in this module.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from pydantic import ValidationError

from novafabric.frontier_safety import (
    FACET_NAME,
    MAX_REF_LENGTH,
    CommitmentBinding,
    ComputedVerdictError,
    FrontierSafetyFacet,
    InvalidReferenceError,
    PayloadCaptureError,
    ThresholdEval,
    VerificationFlags,
    attach_facet,
    build_facet,
    digest_ref,
    facet_from_capsule,
    verify_commitment_binding,
    verify_eval_binding,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO_ROOT / "schemas" / "run-capsule.schema.json"
BASELINE = (
    REPO_ROOT / "tests" / "fixtures" / "model-provenance" / "valid-text-only-capsule.json"
)

EVAL_RECORD = '{"eval":"nf-154-eval-integrity-record"}'
COMMITMENT_TEXT = "RSP v3.0 §4.2 — we will run the ASL-3 threshold eval before deploying"
TRANSCRIPT = "red-team transcript: step 1, the model attempted to escape the sandbox"


@pytest.fixture(scope="module")
def schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text())


@pytest.fixture
def capsule() -> dict[str, Any]:
    return json.loads(BASELINE.read_text())


def _threshold(**kw: object) -> ThresholdEval:
    base: dict[str, object] = {
        "framework": "anthropic_rsp",
        "framework_version": "3.0",
        "threshold_id": "ASL-3",
        "eval_ref": digest_ref(EVAL_RECORD),
    }
    base.update(kw)
    return ThresholdEval(**base)  # type: ignore[arg-type]


def _binding(**kw: object) -> CommitmentBinding:
    base: dict[str, object] = {
        "framework": "anthropic_rsp",
        "commitment_id": "rsp.v3.asl3.pre-deployment-eval",
        "commitment_digest": digest_ref(COMMITMENT_TEXT),
    }
    base.update(kw)
    return CommitmentBinding(**base)  # type: ignore[arg-type]


# ── I-3: never a computed verdict ─────────────────────────────────────────


def test_verdict_defaults_to_null() -> None:
    """The canonical shape: NovaFabric observed the eval ran, forms no judgement."""
    assert _threshold().verdict is None
    assert _binding().verdict is None


def test_verdict_null_alone_is_valid() -> None:
    """Golden: an eval that ran with no verdict recorded at all is a valid object.

    ADR-0167 D1 explicitly wants this — NovaFabric records that the eval ran
    even when the external evaluator's verdict is not (yet) available.
    """
    t = ThresholdEval(
        framework="deepmind_fsf",
        framework_version="2026-04-17",
        threshold_id="fsf.ccl.cbrn",
        eval_ref=digest_ref(EVAL_RECORD),
        verdict=None,
    )
    assert t.verdict is None
    assert t.verdict_ref is None
    assert t.is_evaluated is False


def test_bare_verdict_without_a_verdict_ref_raises() -> None:
    """The heart of the slice.

    A verdict value with no external reference is, once sealed, NovaFabric
    certifying a model. Rejected at construction, not documented in a
    docstring nobody reads at 3am.
    """
    with pytest.raises(ComputedVerdictError, match="verdict_ref"):
        _threshold(verdict="pass")


def test_verdict_with_a_ref_but_no_source_raises() -> None:
    """The second failing direction: attribution must name *who* issued it.

    A ref alone says a document exists; it does not say a named external
    evaluator stands behind the judgement.
    """
    with pytest.raises(ComputedVerdictError, match="verdict_source"):
        _threshold(verdict="pass", verdict_ref=digest_ref("verdict-doc"))


def test_fully_attributed_external_verdict_is_recorded() -> None:
    """The permitted non-null shape: NovaFabric is quoting, not deciding."""
    t = _threshold(
        verdict="threshold_not_met",
        verdict_ref=digest_ref("verdict-doc"),
        verdict_source="rsp_evaluator",
    )
    assert t.verdict == "threshold_not_met"
    assert t.verdict_source == "rsp_evaluator"


def test_verdict_by_reference_without_a_value_is_the_spec_shape() -> None:
    """Spec §4.1: `verdict_ref` set, `verdict: null` — the judgement stays external."""
    t = _threshold(verdict_ref=digest_ref("verdict-doc"), verdict_source="rsp_evaluator")
    assert t.verdict is None
    assert t.is_evaluated is True


def test_the_verdict_invariant_binds_commitment_bindings_too() -> None:
    """Every object in the facet, not just the eval — D4 says "every object"."""
    with pytest.raises(ComputedVerdictError):
        _binding(verdict="satisfied")


def test_falsy_verdicts_are_not_a_loophole() -> None:
    """`verdict: false` / `0` / `""` are values, not absence.

    A truthiness check instead of an `is None` check would let the most
    alarming verdict of all — a bare `false` — through unattributed.
    """
    for value in (False, 0, ""):
        with pytest.raises(ComputedVerdictError):
            _threshold(verdict=value)


def test_verdict_source_cannot_name_novafabric() -> None:
    """There is no `novafabric` verdict source; the value is unspellable."""
    with pytest.raises(ValidationError):
        _threshold(
            verdict="pass",
            verdict_ref=digest_ref("v"),
            verdict_source="novafabric",
        )


def test_commitment_binding_records_no_satisfaction_judgement() -> None:
    """NF-353 records *which* commitment, never *whether* it was satisfied."""
    dumped = _binding().model_dump()
    assert "satisfied" not in dumped
    assert "compliant" not in dumped


# ── I-4: absent is not false ──────────────────────────────────────────────


def test_missing_verdict_is_not_evaluated_not_safe() -> None:
    """A missing verdict means "not evaluated" — never safe, never unsafe."""
    assert _threshold().is_evaluated is False


def test_unchecked_verification_flags_are_none_not_false() -> None:
    """None = not checked; False = checked and failed. The two must not merge."""
    flags = VerificationFlags()
    assert flags.sealed_into_root is None
    assert flags.eval_ref_resolvable is None


def test_unchecked_flags_are_absent_after_serialisation() -> None:
    """The three-state distinction has to survive the dump to be worth anything."""
    out = attach_facet(
        {"run_id": "r"},
        build_facet(threshold_eval=_threshold(), verified=VerificationFlags()),
    )
    assert "sealed_into_root" not in out["facets"][FACET_NAME]["verified"]


def test_eval_ran_is_always_true() -> None:
    """`eval_ran: false` is not expressible: absence of an eval is absence of a facet."""
    with pytest.raises(ValidationError):
        _threshold(eval_ran=False)


# ── I-5: no payloads ──────────────────────────────────────────────────────


def test_raw_bytes_are_rejected_not_silently_hashed() -> None:
    """Hashing for the caller would make smuggling a transcript effortless."""
    with pytest.raises(PayloadCaptureError):
        _threshold(eval_ref=TRANSCRIPT.encode())


def test_inlined_content_over_the_ref_limit_is_rejected() -> None:
    with pytest.raises(PayloadCaptureError):
        _binding(implicated_by_ref="https://x/" + "a" * MAX_REF_LENGTH)


def test_a_ref_must_be_a_digest_or_a_uri() -> None:
    with pytest.raises(InvalidReferenceError):
        _threshold(eval_ref="the eval passed")


def test_commitment_digest_rejects_a_locator() -> None:
    """A URL's text can be edited after the fact; a digest cannot.

    "The commitment we were held to" must bind by content or it binds to
    nothing.
    """
    with pytest.raises(InvalidReferenceError, match="content digest"):
        _binding(commitment_digest="https://example.org/rsp-v3#4.2")


def test_no_payload_reaches_the_serialised_facet() -> None:
    facet = build_facet(
        threshold_eval=_threshold(eval_ref=digest_ref(TRANSCRIPT)),
        commitment_binding=_binding(),
    )
    dumped = facet.model_dump_json()
    assert TRANSCRIPT not in dumped
    assert COMMITMENT_TEXT not in dumped
    assert digest_ref(TRANSCRIPT) in dumped


def test_digest_is_sha256_with_algorithm_prefix() -> None:
    expected = hashlib.sha256(EVAL_RECORD.encode()).hexdigest()
    assert digest_ref(EVAL_RECORD) == f"sha256:{expected}"
    assert digest_ref(EVAL_RECORD) == digest_ref(EVAL_RECORD.encode())


# ── I-2: additive-first, fail-open ────────────────────────────────────────


def test_capsule_without_safety_material_is_untouched() -> None:
    """Golden fixture 1: byte-identical to a capsule from before this feature."""
    original = {"run_id": "r", "facets": {"existing": {"a": 1}}}
    assert attach_facet(original, build_facet()) == original


def test_attach_does_not_mutate_the_input_capsule() -> None:
    original: dict[str, object] = {"run_id": "r"}
    attach_facet(original, build_facet(threshold_eval=_threshold()))
    assert original == {"run_id": "r"}


def test_attach_preserves_sibling_facets() -> None:
    out = attach_facet(
        {"run_id": "r", "facets": {"safety": {"schema_version": "0.1.0"}}},
        build_facet(threshold_eval=_threshold()),
    )
    assert out["facets"]["safety"] == {"schema_version": "0.1.0"}
    assert FACET_NAME in out["facets"]


def test_verification_flags_alone_are_not_material() -> None:
    """A facet saying only "we checked nothing" is a seal surface, not evidence."""
    facet = build_facet(verified=VerificationFlags(sealed_into_root=True))
    assert facet.has_material is False
    assert attach_facet({"run_id": "r"}, facet) == {"run_id": "r"}


def test_facet_carries_a_schema_version() -> None:
    out = attach_facet({"run_id": "r"}, build_facet(threshold_eval=_threshold()))
    assert out["facets"][FACET_NAME]["schema_version"]


def test_facet_round_trips_out_of_a_capsule() -> None:
    facet = build_facet(threshold_eval=_threshold(), commitment_binding=_binding())
    read = facet_from_capsule(attach_facet({"run_id": "r"}, facet))
    assert read is not None
    assert read.threshold_eval is not None
    assert read.threshold_eval.threshold_id == "ASL-3"
    assert read.commitment_binding is not None


def test_reading_a_capsule_without_the_facet_is_not_an_error() -> None:
    """Fail-open: the overwhelmingly common case is "no facet", not a failure."""
    assert facet_from_capsule({"run_id": "r"}) is None
    assert facet_from_capsule({"run_id": "r", "facets": {}}) is None


# ── Schema conformance against the REAL capsule schema ────────────────────


def test_baseline_capsule_is_valid(
    schema: dict[str, Any], capsule: dict[str, Any]
) -> None:
    jsonschema.validate(capsule, schema)


def test_capsule_without_the_facet_still_validates(
    schema: dict[str, Any], capsule: dict[str, Any]
) -> None:
    """Golden fixture 1, against the real schema: no capsule becomes invalid."""
    out = attach_facet(capsule, build_facet())
    assert out == capsule
    assert "facets" not in out
    jsonschema.validate(out, schema)


def test_facet_bearing_capsule_validates_against_the_real_schema(
    schema: dict[str, Any], capsule: dict[str, Any]
) -> None:
    """Golden fixture 2, against the real schema.

    Five earlier facet slices shipped code that `run-capsule.schema.json`
    rejected, because their tests only ever used plain dicts (ADR-0196 D4).
    This validates the shipped builder's own output.
    """
    facet = build_facet(
        threshold_eval=_threshold(
            bound_root=digest_ref("capsule-root"),
            verdict_ref=digest_ref("verdict-doc"),
            verdict_source="rsp_evaluator",
        ),
        commitment_binding=_binding(
            subject_type="incident",
            implicated_by_ref=digest_ref("incident-record"),
        ),
        verified=VerificationFlags(eval_ref_resolvable=True, verdict_by_reference=True),
    )
    out = attach_facet(capsule, facet)
    jsonschema.validate(out, schema)
    assert out["facets"][FACET_NAME]["threshold_eval"]["eval_ran"] is True


def test_facet_name_is_registered_in_the_schema(schema: dict[str, Any]) -> None:
    assert FACET_NAME in schema["properties"]["facets"]["properties"]


# ── Verification helpers ──────────────────────────────────────────────────


def test_eval_binding_verifies_against_the_nf154_record() -> None:
    t = _threshold()
    assert verify_eval_binding(t, EVAL_RECORD) is True
    assert verify_eval_binding(t, EVAL_RECORD + " ") is False


def test_commitment_binding_verifies_against_the_published_text() -> None:
    b = _binding()
    assert verify_commitment_binding(b, COMMITMENT_TEXT) is True
    assert verify_commitment_binding(b, COMMITMENT_TEXT.replace("ASL-3", "ASL-4")) is False


# ── I-1: record-only ──────────────────────────────────────────────────────


def test_module_exposes_no_enforcement_surface() -> None:
    """Record-only is a property of the API, not just of the docs.

    If a block/enforce/gate entry point ever appears here, this fails — which
    is the point: I-1 should be structurally hard to violate.
    """
    import novafabric.frontier_safety as fs

    forbidden = (
        "block",
        "enforce",
        "reject",
        "refuse",
        "quarantine",
        "gate",
        "adjudicate",
        "decide",
        "intervene",
        "apply",
        "run_eval",
    )
    offenders = [
        name
        for name in fs.__all__
        for word in forbidden
        if word in name.lower()
    ]
    assert not offenders, (
        f"frontier_safety must not expose an enforcement or adjudication entry "
        f"point (ADR-0167 I-1): {offenders}"
    )


def test_the_facet_never_asserts_a_model_is_safe() -> None:
    """No field in the serialised facet reads as a NovaFabric safety assertion."""
    facet = FrontierSafetyFacet(
        threshold_eval=_threshold(), commitment_binding=_binding()
    )
    dumped = facet.model_dump_json().lower()
    for word in ("is_safe", "safe_to_deploy", "aligned", "approved", "passed"):
        assert word not in dumped
