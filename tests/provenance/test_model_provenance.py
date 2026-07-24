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

"""ADR-0152 P1 — model-provenance facet + reference binding (NF-201, NF-203).

Tests are organised by the ADR's invariants, because those are what a reviewer
needs to be convinced of: I-1 additive-first, I-2 no payloads, I-3 fail-open,
I-4 records-claims-does-not-adjudicate. The two golden fixtures the ADR names
live under `tests/fixtures/model-provenance/`.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import jsonschema  # type: ignore[import-untyped]
import pytest
from pydantic import ValidationError

from novafabric.provenance import (
    MAX_REF_LENGTH,
    DataCardRef,
    InvalidReferenceError,
    ModelProvenanceFacet,
    PayloadCaptureError,
    VerificationFlags,
    attach_facet,
    build_facet,
    data_card_ref,
    digest_ref,
    facet_from_capsule,
    scan_for_payloads,
    unbound_card_refs,
    verify_artifact_ref,
)

REPO_ROOT = Path(__file__).parents[2]
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "model-provenance"
CAPSULE_SCHEMA = REPO_ROOT / "schemas" / "run-capsule.schema.json"

MANIFEST = '{"nf-055": "model-signing-manifest"}'
CARD_DIGEST = digest_ref("nf-058-dataset-card")
OTHER_DIGEST = digest_ref("another-card")


def _facet(**kw: Any) -> ModelProvenanceFacet:
    base: dict[str, Any] = {
        "model_id": "acme/planner-8b",
        "model_signing_ref": digest_ref(MANIFEST),
    }
    base.update(kw)
    return build_facet(base.pop("model_id"), **base)


# ── Golden fixtures ───────────────────────────────────────────────────────


def test_golden_text_only_capsule_stays_valid_and_untouched() -> None:
    """A capsule with no provenance material must be exactly what it was.

    Validated against the real run-capsule schema before *and* after, so this
    catches the failure mode that matters: a facet writer that leaves a trace
    on capsules that have nothing to say (I-1).
    """
    capsule = json.loads(
        (FIXTURE_DIR / "valid-text-only-capsule.json").read_text(encoding="utf-8")
    )
    schema = json.loads(CAPSULE_SCHEMA.read_text(encoding="utf-8"))
    jsonschema.validate(capsule, schema)

    before = json.dumps(capsule, sort_keys=True)
    out = attach_facet(capsule, build_facet("acme/planner-8b"))

    assert json.dumps(out, sort_keys=True) == before
    jsonschema.validate(out, schema)


def test_golden_facet_round_trips() -> None:
    """The facet fixture parses, re-serialises identically, and keeps its refs."""
    raw = json.loads((FIXTURE_DIR / "valid-facet.json").read_text(encoding="utf-8"))
    facet = ModelProvenanceFacet.model_validate(raw)

    assert facet.model_id == "acme/planner-8b-instruct@2026-06"
    assert len(facet.data_card_refs) == 2
    assert facet.model_dump(exclude_none=True) == raw


def test_golden_facet_carries_no_artifact_content() -> None:
    """Every value in the fixture is an identifier, not a document (I-2)."""
    raw = (FIXTURE_DIR / "valid-facet.json").read_text(encoding="utf-8")
    facet = ModelProvenanceFacet.model_validate(json.loads(raw))
    for ref in (facet.model_signing_ref, facet.slsa_provenance_ref, facet.bound_root):
        assert ref is not None
        assert len(ref) <= MAX_REF_LENGTH


# ── Digest / reference binding (NF-201) ───────────────────────────────────


def test_digest_is_sha256_with_algorithm_prefix() -> None:
    expected = hashlib.sha256(MANIFEST.encode()).hexdigest()
    assert digest_ref(MANIFEST) == f"sha256:{expected}"


def test_digest_accepts_bytes_and_str_identically() -> None:
    assert digest_ref(MANIFEST) == digest_ref(MANIFEST.encode())


def test_reference_verifies_against_the_producer_artifact() -> None:
    facet = _facet()
    assert verify_artifact_ref(facet.model_signing_ref, MANIFEST) is True
    assert verify_artifact_ref(facet.model_signing_ref, MANIFEST + " ") is False


def test_absent_reference_does_not_verify() -> None:
    """An unbound ref is the case the verifier exists to surface, not a pass."""
    assert verify_artifact_ref(None, MANIFEST) is False


def test_uri_reference_does_not_verify_offline() -> None:
    """A locator names where an artifact lives, not what it is.

    Returning True here would report a content binding that was never checked;
    resolving the URI is out of scope for an offline P1.
    """
    facet = _facet(model_signing_ref="https://registry.example/manifest.json")
    assert verify_artifact_ref(facet.model_signing_ref, MANIFEST) is False


@pytest.mark.parametrize(
    "ref",
    [
        "sha256:abc",  # truncated
        "sha256:" + "A" * 64,  # upper-case hex
        "not-a-ref",
        "sha512:" + "a" * 128,  # wrong algorithm
    ],
)
def test_malformed_reference_is_rejected(ref: str) -> None:
    with pytest.raises(ValidationError, match="must be 'sha256:<64 hex>' or a URI"):
        build_facet("m", model_signing_ref=ref)


def test_uri_reference_is_accepted_as_a_locator() -> None:
    facet = build_facet("m", slsa_provenance_ref="oci://acme/attestation:v1")
    assert facet.slsa_provenance_ref == "oci://acme/attestation:v1"


def test_bound_root_must_be_a_digest_not_a_locator() -> None:
    """The seal binding is content identity; a URL can change underneath it."""
    with pytest.raises(ValidationError):
        build_facet("m", model_signing_ref=CARD_DIGEST, bound_root="https://x/root")


# ── I-2: no payloads ──────────────────────────────────────────────────────


def test_raw_bytes_are_refused_not_silently_hashed() -> None:
    """Hashing bytes for the caller would make smuggling weights in effortless."""
    with pytest.raises(ValidationError) as exc:
        build_facet("m", model_signing_ref=b"\x80\x02weights")  # type: ignore[arg-type]
    assert "PayloadCaptureError" in str(exc.value) or "raw bytes" in str(exc.value)


def test_oversized_reference_is_refused_as_inlined_content() -> None:
    with pytest.raises(PayloadCaptureError):
        scan_for_payloads(["sha256:" + "a" * MAX_REF_LENGTH])


def test_payload_scanner_accepts_well_formed_references() -> None:
    scan_for_payloads([CARD_DIGEST, "https://acme.example/card.json"])


def test_payload_scanner_names_the_offending_index() -> None:
    with pytest.raises(InvalidReferenceError, match=r"reference\[1\]"):
        scan_for_payloads([CARD_DIGEST, "garbage"])


def test_serialised_facet_contains_no_artifact_body() -> None:
    facet = _facet(data_card_refs=[data_card_ref(CARD_DIGEST, "2026-04")])
    dumped = facet.model_dump_json()
    assert MANIFEST not in dumped
    assert digest_ref(MANIFEST) in dumped


# ── I-1: additive-first ───────────────────────────────────────────────────


def test_capsule_without_provenance_material_is_untouched() -> None:
    capsule = {"run_id": "r", "facets": {"existing": {"a": 1}}}
    assert attach_facet(capsule, build_facet("acme/planner-8b")) == capsule


def test_model_id_alone_is_not_provenance_material() -> None:
    """The capsule has recorded model_id since v0.2; repeating it answers nothing."""
    assert build_facet("acme/planner-8b").has_material is False
    assert _facet().has_material is True


def test_attach_does_not_mutate_the_input_capsule() -> None:
    capsule: dict[str, Any] = {"run_id": "r", "facets": {"existing": {"a": 1}}}
    snapshot = copy.deepcopy(capsule)
    attach_facet(capsule, _facet())
    assert capsule == snapshot


def test_attach_preserves_sibling_facets() -> None:
    capsule = {"run_id": "r", "facets": {"existing": {"a": 1}}}
    out = attach_facet(capsule, _facet())
    assert out["facets"]["existing"] == {"a": 1}
    assert "model_provenance" in out["facets"]


def test_facet_carries_a_schema_version() -> None:
    out = attach_facet({"run_id": "r"}, _facet())
    assert out["facets"]["model_provenance"]["schema_version"]


def test_card_refs_are_ordered_by_digest_for_a_stable_seal() -> None:
    facet = build_facet(
        "m",
        data_card_refs=[
            data_card_ref(OTHER_DIGEST, "v2"),
            data_card_ref(CARD_DIGEST, "v1"),
        ],
    )
    digests = [r.card_digest for r in facet.data_card_refs]
    assert digests == sorted(digests)


def test_facet_reads_back_out_of_a_capsule() -> None:
    out = attach_facet({"run_id": "r"}, _facet())
    read = facet_from_capsule(out)
    assert read is not None
    assert read.model_signing_ref == digest_ref(MANIFEST)


@pytest.mark.parametrize(
    "capsule",
    [{"run_id": "r"}, {"run_id": "r", "facets": {}}, {"run_id": "r", "facets": None}],
)
def test_reading_a_capsule_without_the_facet_returns_none(
    capsule: dict[str, Any],
) -> None:
    assert facet_from_capsule(capsule) is None


def test_unknown_facet_keys_survive_a_round_trip() -> None:
    """extra='allow' is what lets P2 add checkpoint_chain without a schema break."""
    facet = ModelProvenanceFacet.model_validate(
        {"model_id": "m", "model_signing_ref": CARD_DIGEST, "checkpoint_chain": []}
    )
    assert facet.model_dump(exclude_none=True)["checkpoint_chain"] == []


# ── I-3: fail-open ────────────────────────────────────────────────────────


def test_unresolvable_card_is_recorded_not_raised() -> None:
    ref = data_card_ref(CARD_DIGEST, "v1", resolved=False)
    assert ref.unbound is True
    facet = build_facet("m", data_card_refs=[ref])
    assert unbound_card_refs(facet) == [ref]


def test_resolved_cards_are_not_reported_as_unbound() -> None:
    facet = build_facet("m", data_card_refs=[data_card_ref(CARD_DIGEST, "v1")])
    assert unbound_card_refs(facet) == []


def test_an_unbound_card_still_counts_as_provenance_material() -> None:
    """Knowing which card was wanted is evidence, even when it did not resolve."""
    facet = build_facet(
        "m", data_card_refs=[data_card_ref(CARD_DIGEST, "v1", resolved=False)]
    )
    assert facet.has_material is True
    assert "model_provenance" in attach_facet({"run_id": "r"}, facet)["facets"]


def test_card_ref_requires_a_dataset_version() -> None:
    """An unversioned card ref cannot be reconciled against a chain later."""
    with pytest.raises(ValidationError):
        data_card_ref(CARD_DIGEST, "  ")


def test_card_ref_rejects_a_locator() -> None:
    with pytest.raises(ValidationError):
        data_card_ref("https://acme.example/card.json", "v1")  # type: ignore[arg-type]


# ── I-4: records claims, does not adjudicate ──────────────────────────────


def test_unchecked_verification_flags_are_absent_not_false() -> None:
    """`absent` means not checked; `false` would mean checked and failed.

    P1 performs no signature verification, so a facet it builds must not
    contain signature_ok at all — writing `true` would launder an unperformed
    check into a sealed record.
    """
    out = attach_facet({"run_id": "r"}, _facet())
    block = out["facets"]["model_provenance"]
    assert "verified" not in block


def test_caller_supplied_verification_flags_are_preserved() -> None:
    facet = _facet(verified=VerificationFlags(refs_resolvable=True, signature_ok=False))
    block = attach_facet({"run_id": "r"}, facet)["facets"]["model_provenance"]
    assert block["verified"] == {"refs_resolvable": True, "signature_ok": False}


def test_module_exposes_no_training_or_adjudication_surface() -> None:
    """The hard non-goal is a property of the API, not just of the docs.

    If a train/fine-tune/certify entry point ever appears here this fails,
    which is the point: ADR-0152's in-mission boundary should be structurally
    hard to violate.
    """
    import novafabric.provenance as provenance

    forbidden = {
        "train",
        "finetune",
        "fine_tune",
        "distill",
        "align",
        "serve",
        "certify",
        "scan",
    }
    assert forbidden.isdisjoint({name.lower() for name in provenance.__all__})


def test_data_card_ref_model_is_exported_for_downstream_slices() -> None:
    assert issubclass(DataCardRef, __import__("pydantic").BaseModel)
