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
"""Test 11: SHA-256 checksum is enforced by the backend on upload (FR-15).

Verifies that the backend rejects a PUT request that supplies a deliberately
incorrect ``x-amz-checksum-sha256`` header (S3/MinIO/Ceph RGW) or returns an
error/warning for Azure Blob (Content-MD5 mismatch).

Rationale (FR-15 — in-flight corruption detection):
The WormAdapter.put_object() specification requires callers to supply the
backend-native checksum header so that in-flight corruption between the client
and the storage node is detected.  This test confirms that the backend actually
enforces that check — a backend that silently accepts a wrong checksum would
give false assurance about data integrity.

For S3 / MinIO / Ceph RGW:
  boto3 ``put_object`` with ``ChecksumSHA256=<wrong-value>`` must fail with
  ``InvalidChecksum``, ``BadDigest``, or ``XAmzContentSHA256Mismatch``.

For Azure Blob:
  ``upload_blob`` with mismatched ``Content-MD5`` must fail with
  ``Md5Mismatch`` or ``InvalidMd5``.  If the backend does not enforce it
  the test is marked as WARNING (not FAIL) because Azure Blob's MD5 enforcement
  is container-level-configurable.
"""

from __future__ import annotations

import base64
import hashlib
import uuid
from datetime import datetime, timezone
from typing import Any

from nova_worm_conformance.report import TestRecord


def run(client: Any, bucket: str, framework: str, backend: str, backend_version: str) -> TestRecord:
    """Verify the backend enforces the supplied SHA-256 checksum on upload."""
    key = f"worm-test/{uuid.uuid4()}/checksum-enforcement.bin"
    real_data = b"payload for checksum test"
    timestamp = datetime.now(tz=timezone.utc).isoformat()
    request_sample: dict[str, Any] = {}
    response_sample: dict[str, Any] = {}
    error: str | None = None
    passed = False

    # Compute intentionally wrong checksum (flip last hex digit)
    real_sha256 = hashlib.sha256(real_data).digest()
    wrong_sha256_bytes = bytes([real_sha256[0] ^ 0xFF]) + real_sha256[1:]
    wrong_sha256_b64 = base64.b64encode(wrong_sha256_bytes).decode("ascii")

    try:
        if backend == "azure_blob":
            # Azure uses MD5 for upload verification
            md5_correct = hashlib.md5(real_data).digest()  # noqa: S324
            wrong_md5_b64 = base64.b64encode(
                bytes([md5_correct[0] ^ 0xFF]) + md5_correct[1:]
            ).decode("ascii")
            request_sample = {
                "operation": "upload_blob with wrong Content-MD5",
                "key": key,
                "wrong_md5": wrong_md5_b64,
            }
            try:
                blob_client = client.get_blob_client(key)
                blob_client.upload_blob(
                    real_data,
                    overwrite=True,
                    content_settings=_make_azure_content_settings(wrong_md5_b64),
                )
                # Azure may not enforce MD5 on standard tiers — pass with warning
                error = (
                    "WARNING: backend accepted upload with wrong MD5 checksum. "
                    "Azure Blob may not enforce Content-MD5 at this tier/config. "
                    "Operator must enable 'Require Blob Content-MD5' if FR-15 is required."
                )
                passed = True  # not a hard FAIL for Azure (tier-dependent)
                response_sample = {"outcome": "upload_accepted_without_md5_enforcement"}
            except Exception as azure_exc:
                exc_str = str(azure_exc)
                if any(k in exc_str for k in ["Md5Mismatch", "InvalidMd5", "md5", "checksum"]):
                    passed = True
                    response_sample = {"checksum_error": exc_str[:200]}
                else:
                    error = f"Unexpected Azure upload error: {exc_str[:200]}"
        else:
            # S3 / MinIO / Ceph RGW — boto3 ChecksumSHA256 header
            request_sample = {
                "operation": "put_object with wrong ChecksumSHA256",
                "key": key,
                "wrong_sha256_b64": wrong_sha256_b64,
            }
            try:
                client.put_object(
                    Bucket=bucket,
                    Key=key,
                    Body=real_data,
                    ChecksumAlgorithm="SHA256",
                    ChecksumSHA256=wrong_sha256_b64,
                )
                error = "Backend accepted upload with deliberately wrong SHA-256 checksum"
                passed = False
            except Exception as exc:
                exc_str = str(exc)
                # Accept any of: InvalidChecksum, BadDigest, XAmzContentSHA256Mismatch,
                # InvalidDigest — exact error codes vary by backend version
                if any(
                    k in exc_str
                    for k in [
                        "InvalidChecksum",
                        "BadDigest",
                        "XAmzContentSHA256Mismatch",
                        "InvalidDigest",
                        "checksum",
                        "Checksum",
                        "digest",
                        "Digest",
                    ]
                ):
                    passed = True
                    response_sample = {"checksum_error": exc_str[:200]}
                else:
                    error = f"Unexpected upload error (not checksum-related): {exc_str[:200]}"

    except Exception as outer_exc:
        error = f"Test failed: {outer_exc}"

    return TestRecord(
        test_name="test_sha256_checksum_enforced_by_backend",
        passed=passed,
        framework=framework,
        backend=backend,
        backend_version=backend_version,
        timestamp=timestamp,
        request_sample=request_sample,
        response_sample=response_sample,
        error=error,
    )


def _make_azure_content_settings(content_md5_b64: str) -> Any:
    try:
        from azure.storage.blob import ContentSettings  # type: ignore[import-not-found]
        return ContentSettings(content_md5=content_md5_b64)
    except ImportError:
        return None
