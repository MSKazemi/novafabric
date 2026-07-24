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
"""AwsKmsWrappingBackend — AWS KMS envelope-key wrapping (ADR-0185), moto-tested.

Exercises the AWS KMS `Encrypt`/`Decrypt` wrap path against an in-process AWS
mock (moto), so no live cloud credentials are needed. Skips if moto/boto3 are
unavailable.
"""

from __future__ import annotations

import os

import pytest

moto = pytest.importorskip("moto")
pytest.importorskip("boto3")


@pytest.fixture
def kms_key_id():
    from moto import mock_aws

    with mock_aws():
        import boto3

        client = boto3.client("kms", region_name="us-east-1")
        key = client.create_key(Description="novafabric-test-kek")
        yield key["KeyMetadata"]["KeyId"]


class TestAwsKmsWrappingBackend:
    def test_wrap_unwrap_round_trip(self, kms_key_id) -> None:
        from moto import mock_aws

        from novafabric.trust.novaseal.signing_backend import AwsKmsWrappingBackend

        with mock_aws():
            backend = AwsKmsWrappingBackend(key_id=kms_key_id, region="us-east-1")
            dek = os.urandom(32)
            wrapped = backend.wrap_key(dek)
            assert wrapped != dek  # actually wrapped, not passthrough
            assert backend.unwrap_key(wrapped) == dek

    def test_kek_ref_is_stable_and_nonsecret(self, kms_key_id) -> None:
        from novafabric.trust.novaseal.signing_backend import AwsKmsWrappingBackend

        backend = AwsKmsWrappingBackend(key_id=kms_key_id, region="eu-west-1")
        ref = backend.kek_ref()
        assert ref.startswith("aws-kms:")
        assert "eu-west-1" in ref
        assert backend.kek_ref() == ref  # stable

    def test_satisfies_wrapping_capability(self, kms_key_id) -> None:
        # envelope_encryption gate must accept this backend as wrap-capable.
        from novafabric.trust.envelope_encryption import _require_wrap_capable
        from novafabric.trust.novaseal.signing_backend import AwsKmsWrappingBackend

        backend = AwsKmsWrappingBackend(key_id=kms_key_id, region="us-east-1")
        assert _require_wrap_capable(backend) is backend

    def test_encrypt_blob_round_trip_through_kms(self, kms_key_id) -> None:
        from moto import mock_aws

        from novafabric.trust.envelope_encryption import decrypt_blob, encrypt_blob
        from novafabric.trust.novaseal.signing_backend import AwsKmsWrappingBackend

        with mock_aws():
            backend = AwsKmsWrappingBackend(key_id=kms_key_id, region="us-east-1")
            plaintext = b"sensitive capsule payload"
            blob = encrypt_blob(plaintext, backend=backend)
            assert decrypt_blob(blob, backend=backend) == plaintext
