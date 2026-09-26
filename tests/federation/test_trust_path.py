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

"""ADR-0168 P2 (NF-363) — transitive trust path: walk, fail-closed, adversarial.

Acceptance criteria (spec §3.7, §6):

- AC1 an A→B→C chain terminating at a verifier-pinned anchor passes with
  ``acyclic``, ``no_broken_hop``, ``terminates_at_anchor``.
- AC2 a hop whose signature does not verify under the key the previous hop
  (or the pinned anchor) vouched for fails, naming the broken hop.
- AC3 hop reordering, cycles, unpinned anchors, anchor substitution, key
  confusion, and malformed signatures all fail closed.
- AC4 exceeding a signed / policy delegation depth is flagged, never fatal.
- AC5 oversize paths and malformed encodings are refused at parse time.
- AC6 the path lives additively under ``facets.federation.trust_path``;
  a capsule without one is untouched (I-3).
- AC7 golden fixtures are byte-reproducible from deterministic test keys.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from novafabric.federation import (
    MAX_HOPS,
    PayloadCrossedBoundaryError,
    PinnedAnchor,
    TrustPath,
    TrustPathError,
    attach_facet,
    attach_trust_path,
    build_facet,
    build_trust_anchor,
    facet_from_capsule,
    key_digest,
    parse_trust_path,
    sign_hop,
    trust_path_from_capsule,
    verify_trust_path,
)
from novafabric.federation.facet import InvalidReferenceError
from novafabric.federation.trust_path import (
    IN_MISSION_BOUNDARY,
    statement_bytes,
    structural_summary,
)

from ._trust_path_fixtures import (
    ANCHOR_DIGEST,
    EVIL,
    EVIL_DIGEST,
    FIXTURE_DIR,
    A,
    B,
    C,
    X,
    cases,
    hop_dict,
    valid_path,
)


def _anchors() -> list[PinnedAnchor]:
    return [PinnedAnchor.from_public_key("orgA", A.public_key())]


def _verify(raw: list[dict[str, Any]], **kwargs: Any) -> Any:
    return verify_trust_path(
        parse_trust_path(raw), pinned_anchors=kwargs.pop("anchors", _anchors()), **kwargs
    )


# ── AC7: golden fixtures ──────────────────────────────────────────────────


@pytest.mark.parametrize("name", sorted(cases()))
def test_golden_fixture_is_byte_reproducible(name: str) -> None:
    on_disk = (FIXTURE_DIR / name).read_text(encoding="utf-8")
    assert on_disk == cases()[name], (
        f"{name} drifted; regenerate with python -m tests.federation._trust_path_fixtures"
    )


@pytest.mark.parametrize("name", sorted(cases()))
def test_golden_fixture_verdict(name: str) -> None:
    case = json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))
    anchors = [
        PinnedAnchor(org=a["org"], spki_der=base64.b64decode(a["public_key_spki_b64"]))
        for a in case["anchors"]
    ]
    report = verify_trust_path(parse_trust_path(case["trust_path"]), pinned_anchors=anchors)
    for field, expected in case["expect"].items():
        assert getattr(report, field) == expected, (name, field, report)


# ── AC1: success ──────────────────────────────────────────────────────────


def test_a_b_c_path_to_pinned_anchor_verifies() -> None:
    report = _verify(valid_path())
    assert report.path_walk_ok
    assert report.acyclic and report.no_broken_hop and report.terminates_at_anchor
    assert not report.delegation_depth_exceeded
    assert report.anchor_org == "orgA"
    assert report.anchor_digest == ANCHOR_DIGEST
    assert report.leaf_org == "orgC"
    assert report.leaf_key_digest == key_digest(C.public_key())
    assert report.hop_count == 2


def test_single_hop_path_verifies() -> None:
    assert _verify(valid_path()[:1]).path_walk_ok


def test_ecdsa_p256_hops_verify_and_mix_with_ed25519() -> None:
    p_b = ec.generate_private_key(ec.SECP256R1())
    h0 = sign_hop(
        A,
        from_org="orgA",
        to_org="orgB",
        subject_public_key=p_b.public_key(),
        anchor_digest=ANCHOR_DIGEST,
    )
    h1 = sign_hop(
        p_b,
        from_org="orgB",
        to_org="orgC",
        subject_public_key=C.public_key(),
        anchor_digest=ANCHOR_DIGEST,
    )
    assert _verify([hop_dict(h0), hop_dict(h1)]).path_walk_ok


def test_ecdsa_pinned_anchor() -> None:
    root = ec.generate_private_key(ec.SECP256R1())
    digest = key_digest(root.public_key())
    h0 = sign_hop(
        root,
        from_org="root",
        to_org="orgB",
        subject_public_key=B.public_key(),
        anchor_digest=digest,
    )
    pem = root.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    report = _verify([hop_dict(h0)], anchors=[PinnedAnchor.from_pem("root", pem)])
    assert report.path_walk_ok


def test_verification_is_deterministic() -> None:
    raw = valid_path()
    assert _verify(raw).model_dump() == _verify(raw).model_dump()


# ── AC2/AC3: adversarial ──────────────────────────────────────────────────


def test_signature_by_wrong_key_fails_naming_the_hop() -> None:
    raw = valid_path()
    raw[1] = hop_dict(
        sign_hop(
            X,
            from_org="orgB",
            to_org="orgC",
            subject_public_key=C.public_key(),
            anchor_digest=ANCHOR_DIGEST,
        )
    )
    report = _verify(raw)
    assert not report.path_walk_ok
    assert not report.no_broken_hop
    assert report.broken_hop == 1 and report.reason == "bad_signature"
    assert report.leaf_org is None and report.leaf_key_digest is None


def test_hop_zero_signed_by_impostor_claiming_pinned_anchor_fails() -> None:
    """Anchor substitution at the root: claims the pinned digest, signs with another key."""
    forged = sign_hop(
        EVIL,
        from_org="orgA",
        to_org="orgB",
        subject_public_key=B.public_key(),
        anchor_digest=ANCHOR_DIGEST,
    )
    report = _verify([hop_dict(forged)])
    assert report.reason == "bad_signature" and report.broken_hop == 0


def test_hop_reordering_fails() -> None:
    raw = valid_path()
    report = _verify([raw[1], raw[0]])
    assert not report.path_walk_ok and report.broken_hop == 0


def test_broken_linkage_between_hops_fails() -> None:
    """Hop 1's from_org is not hop 0's to_org even though B's key signed it."""
    raw = valid_path()
    raw[1] = hop_dict(
        sign_hop(
            B,
            from_org="orgZ",
            to_org="orgC",
            subject_public_key=C.public_key(),
            anchor_digest=ANCHOR_DIGEST,
        )
    )
    report = _verify(raw)
    assert report.reason == "broken_linkage" and report.broken_hop == 1


def test_key_confusion_hop_signed_by_anchor_instead_of_delegate_fails() -> None:
    """The anchor's key may only sign hop 0; later hops need the delegate's key."""
    raw = valid_path()
    raw[1] = hop_dict(
        sign_hop(
            A,
            from_org="orgB",
            to_org="orgC",
            subject_public_key=C.public_key(),
            anchor_digest=ANCHOR_DIGEST,
        )
    )
    assert _verify(raw).reason == "bad_signature"


def test_key_confusion_swapped_subject_key_fails() -> None:
    """Swap hop 0's vouched key for C's: the signed statement no longer matches."""
    raw = valid_path()
    c_b64 = raw[1]["subject_public_key"]
    raw[0] = {**raw[0], "subject_public_key": c_b64}
    report = _verify(raw)
    assert report.reason == "statement_digest_mismatch" and report.broken_hop == 0


def test_key_confusion_recomputed_digest_still_fails_signature() -> None:
    raw = valid_path()
    raw[0] = {**raw[0], "subject_public_key": raw[1]["subject_public_key"]}
    import hashlib

    msg = statement_bytes(
        from_org=raw[0]["from_org"],
        to_org=raw[0]["to_org"],
        subject_public_key=raw[0]["subject_public_key"],
        anchor_digest=raw[0]["anchor_digest"],
        max_path_length=None,
    )
    raw[0]["statement_digest"] = f"sha256:{hashlib.sha256(msg).hexdigest()}"
    assert _verify(raw).reason == "bad_signature"


def test_tampered_to_org_fails() -> None:
    raw = valid_path()
    raw[1] = {**raw[1], "to_org": "orgMallory"}
    assert _verify(raw).reason == "statement_digest_mismatch"


def test_stripping_signed_depth_constraint_breaks_the_signature() -> None:
    raw = valid_path(max0=0)
    raw[0] = {k: v for k, v in raw[0].items() if k != "max_path_length"}
    assert _verify(raw).reason == "statement_digest_mismatch"


def test_cycle_by_key_reuse_fails() -> None:
    raw = valid_path()
    raw.append(
        hop_dict(
            sign_hop(
                C,
                from_org="orgC",
                to_org="orgB",
                subject_public_key=B.public_key(),
                anchor_digest=ANCHOR_DIGEST,
            )
        )
    )
    report = _verify(raw)
    assert not report.acyclic and report.reason == "cycle" and report.broken_hop == 2


def test_cycle_back_to_anchor_key_fails() -> None:
    raw = valid_path()
    raw.append(
        hop_dict(
            sign_hop(
                C,
                from_org="orgC",
                to_org="orgA2",
                subject_public_key=A.public_key(),
                anchor_digest=ANCHOR_DIGEST,
            )
        )
    )
    assert _verify(raw).reason == "cycle"


def test_cycle_by_org_id_with_fresh_key_fails() -> None:
    """Org B reappears under a brand-new key: still a cycle by identity."""
    fresh = Ed25519PrivateKey.generate()
    raw = valid_path()
    raw.append(
        hop_dict(
            sign_hop(
                C,
                from_org="orgC",
                to_org="orgB",
                subject_public_key=fresh.public_key(),
                anchor_digest=ANCHOR_DIGEST,
            )
        )
    )
    report = _verify(raw)
    assert not report.path_walk_ok and not report.acyclic
    assert report.reason == "cycle" and report.broken_hop == 2


def test_self_hop_is_a_cycle() -> None:
    h0 = sign_hop(
        A,
        from_org="orgA",
        to_org="orgA",
        subject_public_key=B.public_key(),
        anchor_digest=ANCHOR_DIGEST,
    )
    report = _verify([hop_dict(h0)])
    assert not report.path_walk_ok and report.reason == "cycle"


def test_path_ending_at_unpinned_anchor_fails() -> None:
    h0 = sign_hop(
        EVIL,
        from_org="evil",
        to_org="orgB",
        subject_public_key=B.public_key(),
        anchor_digest=EVIL_DIGEST,
    )
    report = _verify([hop_dict(h0)])
    assert report.reason == "anchor_not_pinned"
    assert not report.terminates_at_anchor and report.anchor_org is None


def test_no_pinned_anchors_means_nothing_verifies() -> None:
    report = _verify(valid_path(), anchors=[])
    assert not report.path_walk_ok and report.reason == "anchor_not_pinned"


def test_right_key_wrong_org_name_is_not_the_anchor() -> None:
    anchors = [PinnedAnchor.from_public_key("orgA-other", A.public_key())]
    assert _verify(valid_path(), anchors=anchors).reason == "anchor_org_mismatch"


def test_anchor_substitution_mid_path_fails() -> None:
    raw = valid_path()
    raw[1] = hop_dict(
        sign_hop(
            B,
            from_org="orgB",
            to_org="orgC",
            subject_public_key=C.public_key(),
            anchor_digest=EVIL_DIGEST,
        )
    )
    both = [*_anchors(), PinnedAnchor.from_public_key("evil", EVIL.public_key())]
    report = _verify(raw, anchors=both)
    assert report.reason == "anchor_mismatch" and not report.terminates_at_anchor


@pytest.mark.parametrize(
    "signature",
    [
        base64.b64encode(b"\x00" * 64).decode(),
        base64.b64encode(b"\x01").decode(),
        base64.b64encode(b"\x30\x06\x02\x01\x01\x02\x01\x01").decode(),
    ],
)
def test_malformed_signature_bytes_fail_closed(signature: str) -> None:
    raw = valid_path()
    raw[0] = {**raw[0], "signature": signature}
    report = _verify(raw)
    assert report.reason == "bad_signature" and report.broken_hop == 0


@pytest.mark.parametrize(
    "signature",
    ["not base64!", "YWJj\n", "YWJ", " YWJj", "YWJj====", "A" * 200, ""],
)
def test_non_base64_signature_is_refused_at_parse(signature: str) -> None:
    raw = valid_path()
    raw[0] = {**raw[0], "signature": signature}
    with pytest.raises(TrustPathError):
        parse_trust_path(raw)


def _hop_vouching_for(der: bytes) -> dict[str, Any]:
    """A hop whose signer (the anchor) validly signs an arbitrary subject key."""
    import hashlib

    b64 = base64.b64encode(der).decode()
    msg = statement_bytes(
        from_org="orgA",
        to_org="orgB",
        subject_public_key=b64,
        anchor_digest=ANCHOR_DIGEST,
        max_path_length=None,
    )
    return {
        "from_org": "orgA",
        "to_org": "orgB",
        "subject_public_key": b64,
        "anchor_digest": ANCHOR_DIGEST,
        "statement_digest": f"sha256:{hashlib.sha256(msg).hexdigest()}",
        "signature": base64.b64encode(A.sign(msg)).decode(),
    }


def test_rsa_subject_key_fails_the_hop() -> None:
    rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=1024)
    der = rsa_key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    report = _verify([_hop_vouching_for(der)])
    assert report.reason == "bad_subject_key" and "unsupported" in (report.detail or "")


def test_oversize_subject_key_is_refused_at_parse() -> None:
    raw = valid_path()
    raw[0] = {**raw[0], "subject_public_key": "A" * 400}
    with pytest.raises(TrustPathError, match="cap"):
        parse_trust_path(raw)


def test_garbage_subject_key_fails_the_walk_after_valid_signature() -> None:
    """A signer can vouch for garbage; the walk stops there, it does not crash."""
    import hashlib

    bogus = base64.b64encode(b"\x00" * 44).decode()
    msg = statement_bytes(
        from_org="orgA",
        to_org="orgB",
        subject_public_key=bogus,
        anchor_digest=ANCHOR_DIGEST,
        max_path_length=None,
    )
    hop = {
        "from_org": "orgA",
        "to_org": "orgB",
        "subject_public_key": bogus,
        "anchor_digest": ANCHOR_DIGEST,
        "statement_digest": f"sha256:{hashlib.sha256(msg).hexdigest()}",
        "signature": base64.b64encode(A.sign(msg)).decode(),
    }
    report = _verify([hop])
    assert report.reason == "bad_subject_key" and report.broken_hop == 0


def test_secp384_subject_key_is_rejected() -> None:
    import hashlib

    k = ec.generate_private_key(ec.SECP384R1()).public_key()
    b64 = base64.b64encode(
        k.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    ).decode()
    msg = statement_bytes(
        from_org="orgA",
        to_org="orgB",
        subject_public_key=b64,
        anchor_digest=ANCHOR_DIGEST,
        max_path_length=None,
    )
    hop = {
        "from_org": "orgA",
        "to_org": "orgB",
        "subject_public_key": b64,
        "anchor_digest": ANCHOR_DIGEST,
        "statement_digest": f"sha256:{hashlib.sha256(msg).hexdigest()}",
        "signature": base64.b64encode(A.sign(msg)).decode(),
    }
    assert _verify([hop]).reason == "bad_subject_key"


# ── AC4: delegation depth is flagged, not fatal ───────────────────────────


def test_signed_depth_constraint_exceeded_is_flagged_not_fatal() -> None:
    report = _verify(valid_path(max0=0))
    assert report.path_walk_ok
    assert report.delegation_depth_exceeded and report.depth_flags == (0,)


def test_signed_depth_constraint_met_is_not_flagged() -> None:
    report = _verify(valid_path(max0=1, max1=0))
    assert report.path_walk_ok and not report.delegation_depth_exceeded


def test_verifier_depth_policy_is_flagged_not_fatal() -> None:
    report = _verify(valid_path(), max_depth=1)
    assert report.path_walk_ok and report.delegation_depth_exceeded
    assert report.depth_flags == (1,)
    assert not _verify(valid_path(), max_depth=2).delegation_depth_exceeded


def test_negative_max_depth_is_refused() -> None:
    with pytest.raises(TrustPathError):
        _verify(valid_path(), max_depth=-1)


# ── strict_depth: signed-constraint overrun made fatal (OpenID Federation) ─


def test_signed_and_policy_overruns_are_reported_apart() -> None:
    signed = _verify(valid_path(max0=0))
    assert signed.signed_depth_flags == (0,) and not signed.policy_depth_exceeded
    policy = _verify(valid_path(), max_depth=1)
    assert policy.signed_depth_flags == () and policy.policy_depth_exceeded
    both = _verify(valid_path(max0=0), max_depth=1)
    assert both.signed_depth_flags == (0,) and both.policy_depth_exceeded
    assert both.depth_flags == (0, 1)


def test_default_is_lenient_per_spec() -> None:
    report = _verify(valid_path(max0=0))
    assert report.path_walk_ok and not report.strict_depth
    assert report.depth_violation_hop is None and report.reason is None


def test_strict_depth_makes_signed_overrun_fatal() -> None:
    report = _verify(valid_path(max0=0), strict_depth=True)
    assert not report.path_walk_ok
    assert report.reason == "signed_depth_exceeded"
    assert report.depth_violation_hop == 1  # hop 1 issued beyond hop 0's allowance of 0
    assert report.strict_depth and report.signed_depth_flags == (0,)
    # The chain itself is intact; the failure is the constraint, not a broken hop.
    assert report.no_broken_hop and report.broken_hop is None
    assert report.leaf_org is None and report.leaf_key_digest is None


def test_strict_depth_does_not_make_policy_depth_fatal() -> None:
    report = _verify(valid_path(), max_depth=1, strict_depth=True)
    assert report.path_walk_ok and report.policy_depth_exceeded


def test_strict_depth_passes_when_signed_constraint_met() -> None:
    assert _verify(valid_path(max0=1, max1=0), strict_depth=True).path_walk_ok


def test_strict_depth_violation_is_the_earliest_overrun() -> None:
    # A single-hop path that met max0=0 still verifies strictly.
    assert _verify(valid_path(max0=0)[:1], strict_depth=True).path_walk_ok
    report = _verify(valid_path(max0=0, max1=0), strict_depth=True)
    assert report.depth_violation_hop == 1


def test_strict_depth_crypto_failure_takes_precedence() -> None:
    raw = valid_path(max0=0)
    raw[1] = {**raw[1], "signature": raw[0]["signature"]}
    report = _verify(raw, strict_depth=True)
    assert report.reason == "bad_signature" and report.depth_violation_hop is None


def test_strict_depth_revoked_takes_precedence() -> None:
    report = _verify(valid_path(max0=0), strict_depth=True, revoked=["orgB"])
    assert report.reason == "path_touches_revoked"
    assert not report.path_walk_ok


# ── Revocation awareness (verifier-supplied; NF-369 record is P5) ─────────


@pytest.mark.parametrize(
    "subject",
    ["orgB", "orgA", "orgC"],
)
def test_path_touching_revoked_org_fails(subject: str) -> None:
    report = _verify(valid_path(), revoked=[subject])
    assert not report.path_walk_ok and report.path_touches_revoked
    assert report.revoked_hits == (subject,)
    assert report.no_broken_hop and report.reason == "path_touches_revoked"


def test_path_touching_revoked_key_digest_fails() -> None:
    digest = key_digest(B.public_key())
    report = _verify(valid_path(), revoked=[digest, "unrelated"])
    assert report.path_touches_revoked and report.revoked_hits == (digest,)


# ── AC5: parse-time refusal ───────────────────────────────────────────────


def test_oversize_path_is_refused() -> None:
    hop = valid_path()[0]
    with pytest.raises(TrustPathError, match="cap"):
        parse_trust_path([hop] * (MAX_HOPS + 1))


def test_max_hops_path_parses() -> None:
    keys = [Ed25519PrivateKey.generate() for _ in range(MAX_HOPS)]
    signer, org, raw = A, "orgA", []
    for i, k in enumerate(keys):
        raw.append(
            hop_dict(
                sign_hop(
                    signer,
                    from_org=org,
                    to_org=f"org{i}",
                    subject_public_key=k.public_key(),
                    anchor_digest=ANCHOR_DIGEST,
                )
            )
        )
        signer, org = k, f"org{i}"
    assert _verify(raw).path_walk_ok


def test_empty_path_is_refused() -> None:
    with pytest.raises(TrustPathError):
        parse_trust_path([])


@pytest.mark.parametrize("raw", [None, "x", 3, b"bytes", {"nothops": []}])
def test_non_list_path_is_refused(raw: object) -> None:
    with pytest.raises(TrustPathError):
        parse_trust_path(raw)


def test_mapping_form_is_accepted() -> None:
    assert len(parse_trust_path({"hops": valid_path()}).hops) == 2


def test_unknown_hop_field_is_refused() -> None:
    raw = valid_path()
    raw[0] = {**raw[0], "trusted": True}
    with pytest.raises(TrustPathError, match="trusted"):
        parse_trust_path(raw)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("from_org", ""),
        ("from_org", "org A"),
        ("from_org", "o" * 257),
        ("to_org", "orgB\n"),
        ("to_org", 7),
        ("anchor_digest", ANCHOR_DIGEST + "\n"),
        ("anchor_digest", ANCHOR_DIGEST.upper()),
        ("statement_digest", "sha256:abc"),
        ("max_path_length", -1),
        ("max_path_length", MAX_HOPS + 1),
        ("subject_public_key", 12),
        ("signature", 12),
    ],
)
def test_malformed_hop_fields_are_refused(field: str, value: object) -> None:
    raw = valid_path()
    raw[0] = {**raw[0], field: value}
    with pytest.raises(TrustPathError):
        parse_trust_path(raw)


def test_private_key_in_subject_field_is_a_boundary_violation() -> None:
    raw = valid_path()
    raw[0] = {**raw[0], "subject_public_key": "-----BEGIN PRIVATE KEY-----"}
    with pytest.raises(PayloadCrossedBoundaryError):
        parse_trust_path(raw)


def test_bytes_org_is_a_boundary_violation() -> None:
    raw = valid_path()
    raw[0] = {**raw[0], "from_org": b"orgA"}
    with pytest.raises(PayloadCrossedBoundaryError):
        parse_trust_path(raw)


def test_pinned_anchor_refuses_private_key_pem() -> None:
    pem = A.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    with pytest.raises(PayloadCrossedBoundaryError):
        PinnedAnchor.from_pem("orgA", pem)


def test_pinned_anchor_refuses_garbage_and_rsa() -> None:
    with pytest.raises(TrustPathError):
        PinnedAnchor.from_pem("orgA", b"not a pem")
    rsa_pem = (
        rsa.generate_private_key(public_exponent=65537, key_size=1024)
        .public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    )
    with pytest.raises(TrustPathError, match="unsupported"):
        PinnedAnchor.from_pem("orgA", rsa_pem)
    with pytest.raises(TrustPathError):
        PinnedAnchor(org="orgA", spki_der=b"\x00\x01")
    with pytest.raises(TrustPathError):
        PinnedAnchor.from_public_key("bad org", A.public_key())


def test_sign_hop_rejects_unsupported_signer_and_bad_inputs() -> None:
    p384 = ec.generate_private_key(ec.SECP384R1())
    with pytest.raises(TrustPathError):
        sign_hop(
            p384,
            from_org="orgA",
            to_org="orgB",  # type: ignore[arg-type]
            subject_public_key=B.public_key(),
            anchor_digest=ANCHOR_DIGEST,
        )
    with pytest.raises(InvalidReferenceError):
        sign_hop(
            A,
            from_org="orgA",
            to_org="orgB",
            subject_public_key=B.public_key(),
            anchor_digest="sha256:nope",
        )


# ── AC6: facet integration, additive ──────────────────────────────────────


def test_capsule_without_path_is_untouched() -> None:
    capsule: dict[str, Any] = {"run_id": "r1"}
    assert attach_trust_path(capsule, None) is capsule
    assert trust_path_from_capsule(capsule) is None
    assert trust_path_from_capsule({"facets": {"federation": {}}}) is None
    assert trust_path_from_capsule({"facets": "junk"}) is None


def test_attach_and_read_back_preserves_p1_facet() -> None:
    pin = build_trust_anchor(
        "spiffe://orgB.example",
        "sha256:" + "aa" * 32,
        bundle_endpoint="https://orgB.example/b",
        endpoint_profile="https_spiffe",
        acquired_at="2027-05-01T00:00:00Z",
    )
    capsule = attach_facet({"run_id": "r1"}, build_facet(trust_anchor=pin))
    path = parse_trust_path(valid_path())
    out = attach_trust_path(capsule, path)
    assert "trust_path" not in capsule["facets"]["federation"]  # input not mutated
    assert trust_path_from_capsule(out) == path
    facet = facet_from_capsule(out)
    assert facet is not None and facet.trust_anchor == pin
    assert isinstance(out["facets"]["federation"]["trust_path"], list)


def test_attach_to_bare_capsule_creates_federation_block() -> None:
    out = attach_trust_path({}, parse_trust_path(valid_path()))
    assert out["facets"]["federation"]["schema_version"]
    assert isinstance(trust_path_from_capsule(out), TrustPath)


def test_malformed_path_in_capsule_raises_on_read() -> None:
    with pytest.raises(TrustPathError):
        trust_path_from_capsule({"facets": {"federation": {"trust_path": "x"}}})


def test_the_facets_own_trust_anchor_is_never_used_as_a_pin() -> None:
    """The verifier's pins are an explicit, required argument — there is no
    code path that reads anchors out of the capsule under verification."""
    import inspect

    sig = inspect.signature(verify_trust_path)
    assert sig.parameters["pinned_anchors"].default is inspect.Parameter.empty


def test_structural_summary_and_boundary_line() -> None:
    rows = structural_summary(parse_trust_path(valid_path()))
    assert [r["to_org"] for r in rows] == ["orgB", "orgC"]
    assert "never a CA" in IN_MISSION_BOUNDARY
    assert "not that the foreign evidence is correct" in IN_MISSION_BOUNDARY
