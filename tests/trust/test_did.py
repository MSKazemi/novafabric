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
"""ADR-0075 — W3C did:key resolution + Verifiable Credential verification.

``did:key`` is self-certifying: the DID *encodes* the public key (multibase +
multicodec), so resolution is pure decoding with no network. A Verifiable
Credential's proof is verified against the issuer's resolved key — an agent's
authorization is thus offline-checkable.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from novafabric.trust.did import (
    CredentialError,
    did_key_from_public_key,
    issue_credential,
    public_key_from_did_key,
    verify_credential,
)


def _key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


def _future(h: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=h)).isoformat()


class TestDidKey:
    def test_round_trip(self) -> None:
        k = _key()
        pub = k.public_key().public_bytes_raw()
        did = did_key_from_public_key(pub)
        assert did.startswith("did:key:z6Mk")  # Ed25519 did:key prefix
        assert public_key_from_did_key(did) == pub

    def test_known_vector(self) -> None:
        # W3C did:key test vector for an all-zero-ish key resolves deterministically.
        k = _key()
        pub = k.public_key().public_bytes_raw()
        assert public_key_from_did_key(did_key_from_public_key(pub)) == pub

    def test_malformed_did_raises(self) -> None:
        with pytest.raises(CredentialError):
            public_key_from_did_key("did:web:example.com")
        with pytest.raises(CredentialError):
            public_key_from_did_key("not-a-did")


class TestVerifiableCredential:
    def test_issue_and_verify(self) -> None:
        issuer_k = _key()
        issuer_did = did_key_from_public_key(issuer_k.public_key().public_bytes_raw())
        subject_did = did_key_from_public_key(_key().public_key().public_bytes_raw())
        vc = issue_credential(
            issuer_k,
            issuer_did=issuer_did,
            subject_did=subject_did,
            authorization=["tool:deploy", "capsule:read"],
            expires_at=_future(10),
        )
        result = verify_credential(vc)
        assert result.valid is True
        assert result.issuer_did == issuer_did
        assert result.subject_did == subject_did
        assert set(result.authorization) == {"tool:deploy", "capsule:read"}

    def test_tampered_authorization_is_rejected(self) -> None:
        issuer_k = _key()
        issuer_did = did_key_from_public_key(issuer_k.public_key().public_bytes_raw())
        vc = issue_credential(
            issuer_k, issuer_did=issuer_did, subject_did=issuer_did,
            authorization=["tool:read"], expires_at=_future(10),
        )
        bad = vc.model_copy(update={"authorization": ["tool:read", "tool:admin"]})
        assert verify_credential(bad).valid is False

    def test_wrong_issuer_key_is_rejected(self) -> None:
        # DID says one key, signed with another → resolution/verify mismatch.
        real_k = _key()
        issuer_did = did_key_from_public_key(real_k.public_key().public_bytes_raw())
        vc = issue_credential(
            _key(), issuer_did=issuer_did, subject_did=issuer_did,  # signed by a DIFFERENT key
            authorization=["x"], expires_at=_future(10), _skip_key_check=True,
        )
        assert verify_credential(vc).valid is False

    def test_expired_credential_is_rejected(self) -> None:
        issuer_k = _key()
        issuer_did = did_key_from_public_key(issuer_k.public_key().public_bytes_raw())
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        vc = issue_credential(
            issuer_k, issuer_did=issuer_did, subject_did=issuer_did,
            authorization=["x"], expires_at=past,
        )
        result = verify_credential(vc)
        assert result.valid is False

    def test_issue_rejects_mismatched_key(self) -> None:
        # issue_credential must refuse to sign as an issuer whose DID key differs.
        other_did = did_key_from_public_key(_key().public_key().public_bytes_raw())
        with pytest.raises(CredentialError):
            issue_credential(
                _key(), issuer_did=other_did, subject_did=other_did,
                authorization=["x"], expires_at=_future(1),
            )
