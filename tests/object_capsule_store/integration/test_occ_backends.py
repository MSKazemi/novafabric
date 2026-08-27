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
"""OCC (If-None-Match: *) integration tests — S3 / MinIO / Ceph RGW (AC-2, BQ-013).

These tests confirm that conditional-PUT semantics work against real backends.
They are gated by NOVA_INTEGRATION=1 and backend-specific env vars.

Env vars:
    NOVA_INTEGRATION=1           — required to run all tests below
    MINIO_ENDPOINT               — MinIO endpoint (default http://localhost:9000)
    MINIO_ACCESS_KEY             — MinIO access key (default minioadmin)
    MINIO_SECRET_KEY             — MinIO secret key (default minioadmin)
    MINIO_BUCKET                 — MinIO bucket (default nova-occ-test)
    S3_BUCKET                    — S3 bucket (gated by NOVA_S3_INTEGRATION=1)
    CEPH_ENDPOINT                — Ceph RGW endpoint (gated by NOVA_CEPH_INTEGRATION=1)
    CEPH_ACCESS_KEY / CEPH_SECRET_KEY / CEPH_BUCKET
"""

from __future__ import annotations

import os
import uuid

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("NOVA_INTEGRATION") != "1",
    reason="Requires NOVA_INTEGRATION=1 and a running object storage backend",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _s3_compatible_client(endpoint_url: str, access_key: str, secret_key: str) -> object:
    """Build a boto3 client for an S3-compatible endpoint.

    The MinIO/Ceph adapters take their credentials from boto3's standard chain,
    so explicit per-backend keys have to be threaded in through a prebuilt
    client rather than constructor kwargs.
    """
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        config=Config(signature_version="s3v4"),
        region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
    )


def _minio_adapter() -> object:
    from novafabric.object_capsule_store.worm.minio import MinioWormAdapter

    endpoint_url = os.environ.get("MINIO_ENDPOINT", "http://localhost:9000")
    return MinioWormAdapter(
        bucket=os.environ.get("MINIO_BUCKET", "nova-occ-test"),
        endpoint_url=endpoint_url,
        client=_s3_compatible_client(
            endpoint_url,
            os.environ.get("MINIO_ACCESS_KEY", "minioadmin"),
            os.environ.get("MINIO_SECRET_KEY", "minioadmin"),
        ),
    )


def _s3_adapter() -> object:
    from novafabric.object_capsule_store.worm.s3 import S3WormAdapter

    return S3WormAdapter(
        bucket=os.environ.get("S3_BUCKET", "nova-occ-test"),
    )


def _ceph_adapter() -> object:
    from novafabric.object_capsule_store.worm.ceph import CephWormAdapter

    endpoint_url = os.environ.get("CEPH_ENDPOINT", "http://localhost:7480")
    return CephWormAdapter(
        bucket=os.environ.get("CEPH_BUCKET", "nova-occ-test"),
        endpoint_url=endpoint_url,
        client=_s3_compatible_client(
            endpoint_url,
            os.environ.get("CEPH_ACCESS_KEY", ""),
            os.environ.get("CEPH_SECRET_KEY", ""),
        ),
    )


def _occ_scenario(adapter: object) -> None:
    """Core OCC scenario: first write succeeds, second write raises ConditionalPutConflict."""
    from novafabric.object_capsule_store.worm.base import ConditionalPutConflict

    key = f"_occ_test/{uuid.uuid4()}/v1.json"
    data = b'{"v":1}'

    # First write must succeed
    adapter.put_log_object_if_absent(key, data)  # type: ignore[union-attr]

    # Second write with the same key must raise ConditionalPutConflict
    with pytest.raises(ConditionalPutConflict, match=key):
        adapter.put_log_object_if_absent(key, b'{"v":2}')  # type: ignore[union-attr]

    # Verify the original data is intact (first write wins)
    stored = adapter.get_object(key)  # type: ignore[union-attr]
    assert stored == data


# ---------------------------------------------------------------------------
# MinIO
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    not os.environ.get("MINIO_ENDPOINT"),
    reason="Set MINIO_ENDPOINT to run MinIO OCC tests",
)
def test_occ_minio_put_log_object_if_absent() -> None:
    """AC-2: Conditional-PUT (If-None-Match: *) confirmed on MinIO."""
    _occ_scenario(_minio_adapter())


# ---------------------------------------------------------------------------
# S3
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    os.environ.get("NOVA_S3_INTEGRATION") != "1",
    reason="Set NOVA_S3_INTEGRATION=1 and S3_BUCKET to run S3 OCC tests",
)
def test_occ_s3_put_log_object_if_absent() -> None:
    """AC-2: Conditional-PUT (If-None-Match: *) confirmed on AWS S3 Object Lock."""
    _occ_scenario(_s3_adapter())


# ---------------------------------------------------------------------------
# Ceph RGW
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    os.environ.get("NOVA_CEPH_INTEGRATION") != "1",
    reason="Set NOVA_CEPH_INTEGRATION=1, CEPH_ENDPOINT, CEPH_ACCESS_KEY, CEPH_SECRET_KEY, "
           "CEPH_BUCKET to run Ceph RGW Pacific+ OCC tests",
)
def test_occ_ceph_rgw_put_log_object_if_absent() -> None:
    """AC-2: Conditional-PUT (If-None-Match: *) confirmed on Ceph RGW Pacific+."""
    _occ_scenario(_ceph_adapter())
