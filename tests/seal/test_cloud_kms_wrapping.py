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
"""Azure Key Vault + GCP Cloud KMS envelope-wrapping backends (ADR-0185).

These verify the integration code against in-memory fakes that implement each
SDK's method contract with a real AES-GCM round-trip. That exercises the
backend's delegation, its wrap/unwrap symmetry, and `kek_ref`; end-to-end against
a live Azure Key Vault / GCP Cloud KMS still requires real credentials.
"""

from __future__ import annotations

import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_FAKE_KEK = b"\x11" * 32


# --------------------------------------------------------------------------- #
# In-memory fakes matching each cloud SDK's client method surface.
# --------------------------------------------------------------------------- #

class _FakeWrapResult:
    def __init__(self, encrypted_key: bytes) -> None:
        self.encrypted_key = encrypted_key


class _FakeUnwrapResult:
    def __init__(self, key: bytes) -> None:
        self.key = key


class FakeAzureCryptographyClient:
    """Mimics azure.keyvault.keys.crypto.CryptographyClient.wrap_key/unwrap_key."""

    def wrap_key(self, algorithm, key):  # noqa: ANN001 - SDK signature
        nonce = os.urandom(12)
        return _FakeWrapResult(nonce + AESGCM(_FAKE_KEK).encrypt(nonce, key, None))

    def unwrap_key(self, algorithm, encrypted_key):  # noqa: ANN001
        return _FakeUnwrapResult(AESGCM(_FAKE_KEK).decrypt(encrypted_key[:12], encrypted_key[12:], None))


class _FakeGcpResp:
    def __init__(self, **kw) -> None:
        self.__dict__.update(kw)


class FakeGcpKmsClient:
    """Mimics google.cloud.kms KeyManagementServiceClient.encrypt/decrypt."""

    def encrypt(self, request):  # noqa: ANN001
        pt = request["plaintext"]
        nonce = os.urandom(12)
        return _FakeGcpResp(ciphertext=nonce + AESGCM(_FAKE_KEK).encrypt(nonce, pt, None))

    def decrypt(self, request):  # noqa: ANN001
        ct = request["ciphertext"]
        return _FakeGcpResp(plaintext=AESGCM(_FAKE_KEK).decrypt(ct[:12], ct[12:], None))


class TestAzureKvWrappingBackend:
    def test_wrap_unwrap_round_trip(self) -> None:
        from novafabric.trust.novaseal.signing_backend import AzureKvWrappingBackend

        backend = AzureKvWrappingBackend(
            key_id="https://vault.vault.azure.net/keys/kek/abc", client=FakeAzureCryptographyClient()
        )
        dek = os.urandom(32)
        wrapped = backend.wrap_key(dek)
        assert wrapped != dek
        assert backend.unwrap_key(wrapped) == dek

    def test_kek_ref(self) -> None:
        from novafabric.trust.novaseal.signing_backend import AzureKvWrappingBackend

        backend = AzureKvWrappingBackend(key_id="https://v.vault.azure.net/keys/k/1", client=object())
        assert backend.kek_ref() == "azure-kv:https://v.vault.azure.net/keys/k/1"

    def test_satisfies_wrapping_capability(self) -> None:
        from novafabric.trust.envelope_encryption import _require_wrap_capable
        from novafabric.trust.novaseal.signing_backend import AzureKvWrappingBackend

        backend = AzureKvWrappingBackend(key_id="x", client=FakeAzureCryptographyClient())
        assert _require_wrap_capable(backend) is backend

    def test_envelope_round_trip(self) -> None:
        from novafabric.trust.envelope_encryption import decrypt_blob, encrypt_blob
        from novafabric.trust.novaseal.signing_backend import AzureKvWrappingBackend

        backend = AzureKvWrappingBackend(key_id="x", client=FakeAzureCryptographyClient())
        pt = b"azure secret payload"
        assert decrypt_blob(encrypt_blob(pt, backend=backend), backend=backend) == pt


class TestGcpKmsWrappingBackend:
    def test_wrap_unwrap_round_trip(self) -> None:
        from novafabric.trust.novaseal.signing_backend import GcpKmsWrappingBackend

        backend = GcpKmsWrappingBackend(
            key_name="projects/p/locations/l/keyRings/r/cryptoKeys/k", client=FakeGcpKmsClient()
        )
        dek = os.urandom(32)
        wrapped = backend.wrap_key(dek)
        assert wrapped != dek
        assert backend.unwrap_key(wrapped) == dek

    def test_kek_ref(self) -> None:
        from novafabric.trust.novaseal.signing_backend import GcpKmsWrappingBackend

        backend = GcpKmsWrappingBackend(key_name="projects/p/locations/l/keyRings/r/cryptoKeys/k", client=object())
        assert backend.kek_ref() == "gcp-kms:projects/p/locations/l/keyRings/r/cryptoKeys/k"

    def test_envelope_round_trip(self) -> None:
        from novafabric.trust.envelope_encryption import decrypt_blob, encrypt_blob
        from novafabric.trust.novaseal.signing_backend import GcpKmsWrappingBackend

        backend = GcpKmsWrappingBackend(key_name="k", client=FakeGcpKmsClient())
        pt = b"gcp secret payload"
        assert decrypt_blob(encrypt_blob(pt, backend=backend), backend=backend) == pt
