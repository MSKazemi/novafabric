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
"""ADR-0055 — x509 certificate-pinned offline signing identity.

Sign with a long-lived key, embed the operator X.509 certificate, and verify offline by
(1) checking the embedded cert is in the operator's *pinned* trust set (by SHA-256
fingerprint) and (2) verifying the signature under the cert's public key. No external
service and no CA path-building — the trust anchor is the pinned fingerprint.
"""

from __future__ import annotations

import datetime

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
)
from cryptography.x509.oid import NameOID

from novafabric.trust.novaseal.x509_identity import (
    X509SigningIdentity,
    verify_x509_signature,
)


def _self_signed(key: object, cn: str = "novaseal-signer") -> bytes:
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    now = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
    builder = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())  # type: ignore[attr-defined]
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=730))
    )
    cert = builder.sign(key, hashes.SHA256())  # type: ignore[arg-type]
    return cert.public_bytes(Encoding.PEM)


def _ecdsa_identity(cn: str = "novaseal-signer") -> tuple[X509SigningIdentity, bytes]:
    key = ec.generate_private_key(ec.SECP256R1())
    key_pem = key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    cert_pem = _self_signed(key, cn)
    return X509SigningIdentity.from_pem(key_pem, cert_pem), cert_pem


class TestSignVerify:
    def test_pinned_signature_verifies(self) -> None:
        identity, _ = _ecdsa_identity()
        sig = identity.sign(b"payload-bytes")
        pinned = {identity.certificate_fingerprint}
        result = verify_x509_signature(b"payload-bytes", sig, pinned_fingerprints=pinned)
        assert result.valid is True
        assert result.subject_common_name == "novaseal-signer"

    def test_tampered_payload_is_rejected(self) -> None:
        identity, _ = _ecdsa_identity()
        sig = identity.sign(b"payload-bytes")
        result = verify_x509_signature(
            b"DIFFERENT", sig, pinned_fingerprints={identity.certificate_fingerprint}
        )
        assert result.valid is False

    def test_unpinned_certificate_is_rejected(self) -> None:
        # A perfectly valid signature, but the cert is not in the operator's trust set.
        identity, _ = _ecdsa_identity()
        sig = identity.sign(b"payload-bytes")
        other, _ = _ecdsa_identity("attacker")
        result = verify_x509_signature(
            b"payload-bytes", sig, pinned_fingerprints={other.certificate_fingerprint}
        )
        assert result.valid is False
        assert "pinned" in result.reason.lower() or "trust" in result.reason.lower()

    def test_rsa_identity_round_trips(self) -> None:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        key_pem = key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
        cert_pem = _self_signed(key)
        identity = X509SigningIdentity.from_pem(key_pem, cert_pem)
        sig = identity.sign(b"rsa-payload")
        assert sig.algorithm.startswith("rsa")
        result = verify_x509_signature(
            b"rsa-payload", sig, pinned_fingerprints={identity.certificate_fingerprint}
        )
        assert result.valid is True

    def test_fingerprint_is_sha256_prefixed(self) -> None:
        identity, _ = _ecdsa_identity()
        fp = identity.certificate_fingerprint
        assert fp.startswith("sha256:")
        assert len(fp) == len("sha256:") + 64

    def test_signature_embeds_the_certificate(self) -> None:
        identity, cert_pem = _ecdsa_identity()
        sig = identity.sign(b"x")
        assert "BEGIN CERTIFICATE" in sig.certificate_pem
        # The embedded cert is the identity's own cert.
        assert sig.certificate_pem.strip() == cert_pem.decode().strip()


def test_empty_pinned_set_rejects_everything() -> None:
    identity, _ = _ecdsa_identity()
    sig = identity.sign(b"payload")
    result = verify_x509_signature(b"payload", sig, pinned_fingerprints=set())
    assert result.valid is False
