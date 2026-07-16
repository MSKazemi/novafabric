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
"""A5 — WORM conformance report *signing honesty* (backlog plan 2026-07-16).

The attestation report must never present a bare hash as a cryptographic
signature. There are exactly two honest outcomes:

* NovaSeal signing backend **and** a key/cert are available  -> a REAL
  ECDSA-P256 signature that verifies against the signing key.
* otherwise -> **no** signature, an honestly-labelled integrity digest
  (``content_sha256``), and a truthful ``signing_status`` / ``signing_detail``.
"""
from __future__ import annotations

import base64
import datetime as _dt
import hashlib
from datetime import datetime, timezone

from nova_worm_conformance.report import build_report
from nova_worm_conformance.signing import apply_signing, sign_report_content


def _make_ec_key_and_cert(tmp_path):
    """Generate an EC P-256 key + self-signed cert; return (key_path, cert_path)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    key_path = tmp_path / "signing_key.pem"
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "worm-test")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(_dt.datetime(2020, 1, 1))
        .not_valid_after(_dt.datetime(2035, 1, 1))
        .sign(key, hashes.SHA256())
    )
    cert_path = tmp_path / "signing_cert.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return key_path, cert_path


def _report():
    return build_report(
        backend="minio",
        endpoint="http://localhost:9000",
        bucket="b",
        framework="sec-17a-4",
        records=[],
        started_at=datetime.now(tz=timezone.utc).isoformat(),
    )


def test_unsigned_path_never_labels_a_hash_as_a_signature():
    body = b'{"hello": "world"}'
    result = sign_report_content(body, key_path=None, cert_path=None)

    assert result.status == "unsigned"
    assert result.signature is None  # the old code stored a base64 hash here — never again
    assert result.method is None
    # an honest integrity digest, correctly labelled as a digest (not a signature)
    assert result.content_sha256 == hashlib.sha256(body).hexdigest()
    assert result.detail and "not" in result.detail.lower()


def test_signed_path_produces_a_real_verifiable_ecdsa_signature(tmp_path):
    key_path, cert_path = _make_ec_key_and_cert(tmp_path)
    body = b'{"hello": "world"}'

    result = sign_report_content(body, key_path=key_path, cert_path=cert_path)

    assert result.status == "signed"
    assert result.method == "novaseal-ecdsa-p256"
    assert result.signature is not None
    assert result.content_sha256 == hashlib.sha256(body).hexdigest()

    # The signature must verify against the signing key over the SHA-256 digest —
    # proving it is genuine ECDSA, not a hash wearing a signature's name.
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ec import ECDSA
    from cryptography.hazmat.primitives.asymmetric.utils import Prehashed
    from cryptography.hazmat.primitives.hashes import SHA256

    pub = serialization.load_pem_private_key(
        key_path.read_bytes(), password=None
    ).public_key()
    sig = base64.b64decode(result.signature)
    digest = hashlib.sha256(body).digest()
    pub.verify(sig, digest, ECDSA(Prehashed(SHA256())))  # raises on an invalid signature
    assert sig != digest  # a bare hash would never verify as an ECDSA signature


def test_apply_signing_sets_report_fields_honestly_when_unsigned():
    report = _report()

    apply_signing(report, key_path=None, cert_path=None)

    assert report.novaseal_signature is None  # never a hash
    assert report.signing_status == "unsigned"
    assert report.content_sha256 is not None  # honest digest still recorded
    d = report.to_dict()
    assert d["novaseal_signature"] is None
    assert d["signing_status"] == "unsigned"
    assert d["content_sha256"] == report.content_sha256


def test_signable_bytes_exclude_signing_fields(tmp_path):
    # Signing is computed over content that excludes the signing block, so applying
    # a signature does not change the bytes that were signed.
    report = _report()
    before = report.signable_bytes()

    key_path, cert_path = _make_ec_key_and_cert(tmp_path)
    apply_signing(report, key_path=key_path, cert_path=cert_path)

    after = report.signable_bytes()
    assert before == after
    assert report.signing_status == "signed"
    assert report.content_sha256 == hashlib.sha256(before).hexdigest()
