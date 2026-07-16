"""ADR-0169 D1 / NF-375 — journalism/whistleblower tamper-evident, source-protecting attestation.

A statement over an already-sealed bundle that proves authenticity **without revealing the source**:
a ``content_digest``, an ``authenticity_attestation`` (a reference to the bundle's existing
Evidence-Bundle Ed25519 signature — this slice never signs), and an optional ``anonymity_set_ref``.
The hard invariant (I-5): it **MUST NOT** capture or embed source identity, contact, or routing
metadata — the validator rejects any supplied field matching such a shape.
"""
from __future__ import annotations

import pytest

from novafabric.compliance.export.public._whistleblower import (
    WhistleblowerAttestation,
    build_whistleblower_attestation,
    source_identifying_fields,
)

_VALID = {
    "content_digest": "sha256:" + "a" * 64,
    "authenticity_attestation": "bundle://root/sig#ed25519",
    "anonymity_set_ref": "anonset://group/42",
}


def test_valid_attestation_binds_digest_and_signature():
    att = build_whistleblower_attestation(_VALID)
    assert isinstance(att, WhistleblowerAttestation)
    assert att.content_digest == _VALID["content_digest"]
    assert att.authenticity_attestation == _VALID["authenticity_attestation"]
    assert att.anonymity_set_ref == "anonset://group/42"


def test_anonymity_set_ref_is_optional():
    att = build_whistleblower_attestation(
        {"content_digest": "d", "authenticity_attestation": "s"}
    )
    assert att.anonymity_set_ref is None


@pytest.mark.parametrize(
    "bad_field",
    [
        "submitter_email",
        "source_name",
        "contact_phone",
        "ip_address",
        "routing_info",
        "sender",
        "reporter_identity",
        "full_name",
    ],
)
def test_rejects_any_source_identifying_field(bad_field):
    doc = dict(_VALID)
    doc[bad_field] = "leaked"
    with pytest.raises(ValueError) as exc:
        build_whistleblower_attestation(doc)
    assert bad_field in str(exc.value)  # the error names the offending field


def test_source_identifying_fields_flags_the_bad_keys():
    flagged = source_identifying_fields(
        {"content_digest": "d", "source_email": "x", "handle": "y", "authenticity_attestation": "s"}
    )
    assert set(flagged) == {"source_email", "handle"}


def test_missing_required_fields_raise():
    with pytest.raises(ValueError):
        build_whistleblower_attestation({"content_digest": "d"})  # no authenticity_attestation
    with pytest.raises(ValueError):
        build_whistleblower_attestation({"authenticity_attestation": "s"})  # no content_digest


def test_model_has_no_source_identity_field():
    for forbidden in ("source", "submitter", "identity", "contact", "email", "routing", "name"):
        assert not any(forbidden in f for f in WhistleblowerAttestation.model_fields)
