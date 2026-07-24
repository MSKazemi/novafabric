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

"""ADR-0168 P1 (NF-361/NF-362) — federated exchange + trust-anchor pin.

Organised by the ADR's invariants rather than by function, because the
invariants are what the slice exists to hold: I-1 record-only, I-2
references-and-digests-only, I-3 fail-open/additive-first, I-4
absent-is-not-false — plus the two properties whose failure modes are silent
and therefore get sections of their own: the no-shared-backend reference
semantics, and the absence of any path walk.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from pydantic import ValidationError

from novafabric.federation import (
    FACET_NAME,
    MAX_REF_LENGTH,
    ExchangeManifest,
    FederationFacet,
    FederationVerification,
    InvalidReferenceError,
    PayloadCrossedBoundaryError,
    TrustAnchorPin,
    anchor_state,
    attach_facet,
    build_exchange,
    build_facet,
    build_trust_anchor,
    digest_artifact,
    facet_from_capsule,
    reference_state,
    scan_for_payload,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO_ROOT / "schemas" / "run-capsule.schema.json"
BASELINE = (
    REPO_ROOT / "tests" / "fixtures" / "model-provenance" / "valid-text-only-capsule.json"
)

ORG_B = "orgkey:sha256:" + "b0" * 32
DOMAIN_B = "spiffe://orgB.example"
DOMAIN_C = "spiffe://orgC.example"
BUNDLE_DIGEST = "sha256:" + "9a" * 32
ANCHOR_DIGEST = "sha256:" + "aa" * 32
CAPSULE_REF = "capsule://sha256:" + "7c" * 32


@pytest.fixture(scope="module")
def schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text())


@pytest.fixture
def capsule() -> dict[str, Any]:
    return json.loads(BASELINE.read_text())


def _exchange(**overrides: Any) -> ExchangeManifest:
    kwargs: dict[str, Any] = {
        "source_org": ORG_B,
        "bundle_digest": BUNDLE_DIGEST,
        "no_shared_backend": True,
        "import_refs": [CAPSULE_REF],
        "exchange_signature": "dsse:sig-over-manifest",
    }
    kwargs.update(overrides)
    return build_exchange(
        kwargs.pop("source_org"), kwargs.pop("bundle_digest"), **kwargs
    )


def _pin(**overrides: Any) -> TrustAnchorPin:
    kwargs: dict[str, Any] = {
        "foreign_trust_domain": DOMAIN_B,
        "trust_bundle_digest": ANCHOR_DIGEST,
        "bundle_endpoint": "https://orgB.example/.well-known/spiffe-bundle",
        "endpoint_profile": "https_spiffe",
        "acquired_at": "2027-05-01T00:00:00Z",
    }
    kwargs.update(overrides)
    return build_trust_anchor(
        kwargs.pop("foreign_trust_domain"), kwargs.pop("trust_bundle_digest"), **kwargs
    )


# ── Hash construction: plain sha256, no tree ──────────────────────────────


def test_digest_is_raw_sha256_of_the_artifact_bytes() -> None:
    """Pins the construction against both of the repo's Merkle modules.

    `evidence/merkle.py` prefixes leaves (RFC 6962) and
    `trust/novaseal/merkle.py` pads pairwise; either would produce a different
    value here. Asserting bit-identity with raw sha256 means a tree cannot be
    introduced later without this failing.
    """
    content = b"org B's sealed bundle"
    assert digest_artifact(content) == "sha256:" + hashlib.sha256(content).hexdigest()


def test_digest_accepts_str_and_bytes_identically() -> None:
    assert digest_artifact("abc") == digest_artifact(b"abc")


def test_digest_does_not_canonicalise_json() -> None:
    """A foreign artifact is bound as bytes, not as re-serialised structure.

    Two JSON encodings of the same object are different artifacts, and the
    exporting org signed one of them.
    """
    assert digest_artifact('{"a":1}') != digest_artifact('{ "a": 1 }')


def test_digest_matches_the_manifests_bundle_digest_when_bytes_are_held() -> None:
    """The one legitimate local computation in a reference import."""
    content = b"org B's sealed bundle"
    manifest = _exchange(bundle_digest=digest_artifact(content))
    assert digest_artifact(content) == manifest.bundle_digest


# ── no_shared_backend: a reference import is not a verification ───────────


def test_no_shared_backend_has_no_default() -> None:
    """D1's load-bearing claim must be made deliberately, never inherited."""
    with pytest.raises(ValidationError):
        ExchangeManifest(source_org=ORG_B, bundle_digest=BUNDLE_DIGEST)


def test_reference_state_has_no_verified_outcome() -> None:
    """Holding a digest cannot produce a content verdict, so no member says it."""
    from novafabric.federation.facet import ReferenceState

    members = set(ReferenceState.__args__)  # type: ignore[attr-defined]
    assert members == {"bound", "unbound"}


def test_a_resolvable_ref_is_bound_not_verified() -> None:
    manifest = _exchange()
    assert reference_state(manifest, CAPSULE_REF, resolvable=[CAPSULE_REF]) == "bound"


def test_an_unresolvable_ref_is_unbound(  # I-4
) -> None:
    """The normal state of a no-shared-backend exchange, not a finding."""
    manifest = _exchange()
    assert reference_state(manifest, CAPSULE_REF, resolvable=[]) == "unbound"


def test_a_ref_absent_from_the_manifest_is_unbound_not_an_error() -> None:
    """A verifier sweeping many manifests must be able to collect answers."""
    manifest = _exchange(import_refs=[])
    assert reference_state(manifest, CAPSULE_REF, resolvable=[CAPSULE_REF]) == "unbound"


def test_the_facet_exposes_no_foreign_contents() -> None:
    """No field anywhere in the facet can carry the foreign bundle's bytes.

    The structural version of the docstring's promise: if a `contents`,
    `payload`, or `body` field is ever added, this fails.
    """
    forbidden = {"contents", "content", "payload", "body", "bytes", "data", "raw"}
    for model in (ExchangeManifest, TrustAnchorPin, FederationFacet):
        assert forbidden.isdisjoint(model.model_fields), (
            f"{model.__name__} must not carry foreign payload (ADR-0168 I-2)"
        )


def test_refs_resolvable_is_not_named_refs_verified() -> None:
    """Locating an artifact is not inspecting it; the field name must not blur that."""
    assert "refs_resolvable" in FederationVerification.model_fields
    assert "refs_verified" not in FederationVerification.model_fields


# ── No path walk in P1 (NF-363 is P2) ─────────────────────────────────────


def test_a_pin_naming_org_b_yields_no_conclusion_about_org_c() -> None:
    """The transitive-trust guard. P1 pins; it never composes.

    A caller holding a facet that pins org B must not be able to conclude
    anything about org C from it — not `trusted`, and not `untrusted` either.
    """
    facet = build_facet(trust_anchor=_pin(foreign_trust_domain=DOMAIN_B))
    assert facet is not None
    assert anchor_state(facet, DOMAIN_B) == "pinned"
    assert anchor_state(facet, DOMAIN_C) == "unknown"


def test_anchor_state_has_no_trusted_or_untrusted_outcome() -> None:
    """A pin records an act, never a verdict (I-1)."""
    from novafabric.federation.facet import AnchorState

    members = set(AnchorState.__args__)  # type: ignore[attr-defined]
    assert members == {"pinned", "unknown"}
    assert "trusted" not in members
    assert "untrusted" not in members


@pytest.mark.parametrize(
    "lookalike",
    [
        "spiffe://evil-orgB.example",
        "spiffe://orgB.example.attacker.tld",
        "orgB.example",
        "spiffe://orgb.example",
    ],
)
def test_a_lookalike_domain_is_never_pinned(lookalike: str) -> None:
    """Exact equality only — a suffix or prefix heuristic hands an attacker a
    pin they earned by choosing a name."""
    facet = build_facet(trust_anchor=_pin())
    assert facet is not None
    assert anchor_state(facet, lookalike) == "unknown"


def test_the_facet_carries_no_path_or_hop_surface() -> None:
    """P2's shape must be absent, not merely unpopulated."""
    assert "trust_path" not in FederationFacet.model_fields
    assert "hops" not in FederationFacet.model_fields
    # `trust_anchor` is singular: a list is the shape from which a caller
    # starts composing a chain.
    annotation = str(FederationFacet.model_fields["trust_anchor"].annotation)
    assert "list" not in annotation.lower()


def test_the_package_exports_no_path_walker() -> None:
    import novafabric.federation as federation

    lowered = {name.lower() for name in federation.__all__}
    for fragment in ("walk", "transitive", "path", "chain", "delegat"):
        assert not any(fragment in name for name in lowered), (
            f"no {fragment!r} entry point until NF-363/P2 (ADR-0168)"
        )


# ── I-1: record-only ──────────────────────────────────────────────────────


def test_package_exposes_no_adjudication_entry_point() -> None:
    """Record-only is a property of the API, not just of the docs.

    NovaFabric records who pinned what. If an entry point ever appears that
    decides whether a foreign org is trustworthy — or that acts as a CA, IdP,
    or notary — this fails, which is the point.
    """
    import novafabric.federation as federation

    forbidden = {
        "trust",
        "distrust",
        "untrust",
        "approve",
        "accept",
        "reject",
        "deny",
        "enforce",
        "vouch",
        "certify",
        "issue",
        "revoke",
        "authorize",
        "authorise",
        "adjudicate",
    }
    exported = {name.lower() for name in federation.__all__}
    assert forbidden.isdisjoint(exported), (
        "federation must not expose a trust-adjudication entry point "
        "(ADR-0168 in-mission boundary, I-1)"
    )


def test_a_pin_is_not_an_endorsement_of_the_foreign_roots() -> None:
    """The pin records a digest; it asserts nothing about the roots' fitness."""
    pin = _pin()
    assert pin.trust_bundle_digest == ANCHOR_DIGEST
    assert not hasattr(pin, "valid")
    assert not hasattr(pin, "trusted")


# ── I-2: references and digests only ──────────────────────────────────────


def test_raw_bytes_are_refused_where_a_reference_belongs() -> None:
    with pytest.raises(PayloadCrossedBoundaryError, match="not raw bytes"):
        _exchange(source_org=b"\x00\x01")


def test_import_refs_refuses_raw_bytes() -> None:
    with pytest.raises(PayloadCrossedBoundaryError, match="not raw bytes"):
        _exchange(import_refs=b"a-whole-bundle")


def test_a_nested_container_is_refused_as_foreign_payload() -> None:
    with pytest.raises(PayloadCrossedBoundaryError, match="not a container"):
        _exchange(import_refs=[{"capsule": "inlined"}])


@pytest.mark.parametrize(
    "key_material",
    [
        "-----BEGIN PRIVATE KEY-----\nMIIE...\n-----END PRIVATE KEY-----",
        "-----BEGIN RSA PRIVATE KEY-----\nMIIE...",
        "-----BEGIN OPENSSH PRIVATE KEY-----\nb3Bl...",
        "-----BEGIN PGP PRIVATE KEY BLOCK-----",
    ],
)
def test_private_key_material_never_crosses_the_boundary(key_material: str) -> None:
    """The worst available I-2 failure: irreversible once sealed."""
    with pytest.raises(PayloadCrossedBoundaryError, match="private key material"):
        _exchange(exchange_signature=key_material)


def test_an_oversized_reference_is_refused_as_inlined_payload() -> None:
    with pytest.raises(PayloadCrossedBoundaryError, match="reference limit"):
        _exchange(import_refs=["capsule://" + "x" * MAX_REF_LENGTH])


def test_scan_for_payload_lets_a_caller_check_before_constructing() -> None:
    scan_for_payload([ORG_B, CAPSULE_REF])  # does not raise
    with pytest.raises(PayloadCrossedBoundaryError):
        scan_for_payload([ORG_B, b"raw"])


@pytest.mark.parametrize(
    "bad",
    ["sha256:short", "SHA256:" + "AB" * 32, "9a" * 32, "sha512:" + "9a" * 32],
)
def test_a_malformed_digest_fails_at_construction(bad: str) -> None:
    """Not years later, during an audit of an exchange nobody remembers."""
    with pytest.raises(InvalidReferenceError, match="sha256:<64 hex>"):
        _exchange(bundle_digest=bad)


def test_an_empty_reference_is_refused() -> None:
    with pytest.raises(InvalidReferenceError, match="non-empty"):
        _exchange(source_org="   ")


def test_a_non_https_bundle_endpoint_is_refused() -> None:
    """Both NF-362 profiles are https_*; an http fetch was not federated."""
    with pytest.raises(InvalidReferenceError, match="https://"):
        _pin(bundle_endpoint="http://orgB.example/.well-known/spiffe-bundle")


def test_a_pin_without_an_acquisition_time_is_refused() -> None:
    with pytest.raises(ValidationError):
        _pin(acquired_at="  ")


def test_an_unknown_endpoint_profile_is_refused() -> None:
    with pytest.raises(ValidationError):
        _pin(endpoint_profile="http_plain")


# ── I-3: additive-first and fail-open ─────────────────────────────────────


def test_capsule_without_federation_material_is_untouched(
    capsule: dict[str, Any],
) -> None:
    """Golden fixture 1: a local-only capsule is returned byte-identical."""
    before = json.dumps(capsule, sort_keys=True)
    out = attach_facet(capsule, build_facet())
    assert out is capsule
    assert json.dumps(out, sort_keys=True) == before
    assert "facets" not in out


def test_build_facet_returns_none_when_nothing_was_federated() -> None:
    """An empty facet would assert an exchange nobody made."""
    assert build_facet() is None


def test_a_verification_block_alone_does_not_produce_a_facet() -> None:
    """It would describe checks on nothing."""
    assert build_facet(verified=FederationVerification(exchange_sig_ok=True)) is None


def test_attach_does_not_mutate_the_input_capsule(capsule: dict[str, Any]) -> None:
    before = json.dumps(capsule, sort_keys=True)
    attach_facet(capsule, build_facet(exchange=_exchange()))
    assert json.dumps(capsule, sort_keys=True) == before


def test_attach_preserves_sibling_facets(capsule: dict[str, Any]) -> None:
    capsule["facets"] = {"safety": {"schema_version": "0.1.0"}}
    out = attach_facet(capsule, build_facet(exchange=_exchange()))
    assert set(out["facets"]) == {"safety", FACET_NAME}


def test_facet_round_trips_through_a_capsule(capsule: dict[str, Any]) -> None:
    facet = build_facet(exchange=_exchange(), trust_anchor=_pin())
    out = attach_facet(capsule, facet)
    read = facet_from_capsule(out)
    assert read is not None
    assert read.exchange is not None
    assert read.exchange.bundle_digest == BUNDLE_DIGEST
    assert read.trust_anchor is not None
    assert read.trust_anchor.foreign_trust_domain == DOMAIN_B


def test_reading_a_capsule_without_the_facet_is_not_an_error(
    capsule: dict[str, Any],
) -> None:
    assert facet_from_capsule(capsule) is None
    assert facet_from_capsule({"facets": "not-a-dict"}) is None
    assert facet_from_capsule({"facets": {}}) is None


def test_facet_carries_a_schema_version() -> None:
    facet = build_facet(exchange=_exchange())
    assert facet is not None
    assert facet.schema_version == "0.1.0"


# ── I-4: absent is not false ──────────────────────────────────────────────


def test_an_unchecked_verification_is_none_not_false() -> None:
    """An unchecked exchange must stay distinguishable from a detected forgery."""
    verified = FederationVerification()
    assert verified.exchange_sig_ok is None
    assert verified.refs_resolvable is None
    assert verified.sealed_into_root is None


def test_a_failed_check_is_distinguishable_from_an_unchecked_one() -> None:
    unchecked = FederationVerification()
    failed = FederationVerification(exchange_sig_ok=False)
    assert unchecked.exchange_sig_ok is not failed.exchange_sig_ok


def test_unchecked_fields_are_absent_from_the_serialised_facet(
    capsule: dict[str, Any],
) -> None:
    """`null` would re-collapse the distinction the tri-state exists to keep."""
    facet = build_facet(
        exchange=_exchange(),
        verified=FederationVerification(exchange_sig_ok=True),
    )
    out = attach_facet(capsule, facet)
    block = out["facets"][FACET_NAME]
    assert block["verified"] == {"exchange_sig_ok": True}
    assert "refs_resolvable" not in block["verified"]


def test_an_absent_signature_is_absent_not_null(capsule: dict[str, Any]) -> None:
    out = attach_facet(
        capsule, build_facet(exchange=_exchange(exchange_signature=None))
    )
    assert "exchange_signature" not in out["facets"][FACET_NAME]["exchange"]


def test_an_unpinned_facet_reports_unknown_not_untrusted() -> None:
    facet = build_facet(exchange=_exchange())
    assert facet is not None
    assert anchor_state(facet, DOMAIN_B) == "unknown"


# ── Real-schema validation (ADR-0196 boundary) ────────────────────────────


def test_local_only_capsule_fixture_validates(
    schema: dict[str, Any], capsule: dict[str, Any]
) -> None:
    """Golden fixture 1: the local-only capsule is still valid."""
    assert "facets" not in capsule
    jsonschema.validate(capsule, schema)


def test_federation_bearing_capsule_validates_against_the_real_schema(
    schema: dict[str, Any], capsule: dict[str, Any]
) -> None:
    """Golden fixture 2: a valid federation facet.

    Uses the shipped builders rather than a hand-written dict, because a
    hand-written dict is what let the original ADR-0196 gap through.
    """
    out = attach_facet(
        capsule,
        build_facet(
            exchange=_exchange(),
            trust_anchor=_pin(),
            verified=FederationVerification(
                exchange_sig_ok=True, refs_resolvable=False
            ),
        ),
    )
    assert FACET_NAME in out["facets"]
    jsonschema.validate(out, schema)


def test_facet_name_matches_the_schema_registry() -> None:
    registered = json.loads(SCHEMA_PATH.read_text())["properties"]["facets"][
        "properties"
    ]
    assert FACET_NAME in registered
