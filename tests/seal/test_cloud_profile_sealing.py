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
"""A cloud signing profile must actually seal through its SigningBackend.

`build_signing_backend()` existed, was unit-tested, and had **no production
caller**: nothing ever passed `backend=` to `create_envelope()`. `NovaSeal.seal()`
handed it `key_path`/`cert_path` instead, so every cloud profile (`aws_kms`,
`azure_kv`, `gcp_kms`) fell into the local-PEM branch with no private key and
failed with `Failed to load signing material: [Errno 2] No such file or
directory: 'None'`. Bring-your-own-KMS could not seal a capsule at all.

Found 2026-08-27 by configuring `profile: azure_kv` against a live Key Vault and
running `nova capture`. These tests need no cloud: they assert that a cloud
profile routes through the backend and never reads a local key.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from novafabric.trust.novaseal import KeyConfig, NovaSeal


@pytest.fixture()
def signing_material(tmp_path: Path) -> tuple[ec.EllipticCurvePrivateKey, Path]:
    """A P-256 key plus a self-signed cert written to disk (cert only)."""
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "novaseal-cloud-test")])
    import datetime

    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    cert_path = tmp_path / "cert.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return key, cert_path


class _RecordingBackend:
    """Stand-in for AzureKv/AwsKms/GcpKms: signs in-process, records the call."""

    def __init__(self, key: ec.EllipticCurvePrivateKey, cert_path: Path) -> None:
        self._key = key
        self._cert_der = x509.load_pem_x509_certificate(
            cert_path.read_bytes()
        ).public_bytes(serialization.Encoding.DER)
        self.digests: list[bytes] = []

    def sign_digest(self, digest: bytes) -> bytes:
        self.digests.append(digest)
        return self._key.sign(digest, ec.ECDSA(hashes.SHA256()))

    def get_cert_der(self) -> bytes:
        return self._cert_der


def _seal(config: KeyConfig, tmp_path: Path, backend: Any) -> Any:
    seal = NovaSeal(
        config=config,
        tsa_url="",  # no timestamping in unit tests
        db_path=str(tmp_path / "merkle.db"),
        backend=backend,
    )
    return seal.seal({"run_id": "01TEST", "schema_version": "1.0"})


@pytest.mark.parametrize("profile", ["azure_kv", "aws_kms", "gcp_kms"])
def test_cloud_profile_seals_through_backend_without_a_local_key(
    profile: str,
    tmp_path: Path,
    signing_material: tuple[ec.EllipticCurvePrivateKey, Path],
) -> None:
    """Regression: this raised "No such file or directory: 'None'"."""
    key, cert_path = signing_material
    backend = _RecordingBackend(key, cert_path)

    # A cloud profile has no local key; callers stringify the absent path.
    config = KeyConfig(profile=profile, key_path="None", cert_path=str(cert_path))
    bundle = _seal(config, tmp_path, backend)

    assert backend.digests, "the backend was never asked to sign"
    envelope = json.loads(bundle.dsse_envelope)
    assert envelope["signatures"], "envelope carries no signature"


def test_backend_signs_the_pae_digest_not_the_raw_payload(
    tmp_path: Path,
    signing_material: tuple[ec.EllipticCurvePrivateKey, Path],
) -> None:
    """The digest handed to a KMS must be SHA-256 over the DSSE PAE encoding."""
    key, cert_path = signing_material
    backend = _RecordingBackend(key, cert_path)
    config = KeyConfig(profile="azure_kv", key_path="None", cert_path=str(cert_path))

    bundle = _seal(config, tmp_path, backend)

    import base64

    from novafabric.trust.novaseal.envelope import _pae

    envelope = json.loads(bundle.dsse_envelope)
    payload = base64.b64decode(envelope["payload"])
    expected = hashlib.sha256(_pae(envelope["payloadType"], payload)).digest()
    assert backend.digests == [expected]


def test_local_profile_still_uses_the_on_disk_key(
    tmp_path: Path,
    signing_material: tuple[ec.EllipticCurvePrivateKey, Path],
) -> None:
    """The local path must be untouched by the backend wiring."""
    key, cert_path = signing_material
    key_path = tmp_path / "key.pem"
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    config = KeyConfig(
        profile="local", key_path=str(key_path), cert_path=str(cert_path)
    )
    bundle = _seal(config, tmp_path, backend=None)
    assert json.loads(bundle.dsse_envelope)["signatures"]
