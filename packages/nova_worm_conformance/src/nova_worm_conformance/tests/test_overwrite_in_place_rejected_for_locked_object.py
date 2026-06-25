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
"""Test 9: Overwrite in-place is rejected for COMPLIANCE-locked objects."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from nova_worm_conformance.report import TestRecord


def run(client: Any, bucket: str, framework: str, backend: str, backend_version: str) -> TestRecord:
    """Attempt to overwrite a COMPLIANCE-locked object → expect error."""
    import uuid
    key = f"worm-test/{uuid.uuid4()}/overwrite.bin"
    original = b"original locked content"
    overwrite = b"attempted overwrite content"
    timestamp = datetime.now(tz=timezone.utc).isoformat()
    request_sample: dict[str, Any] = {}
    response_sample: dict[str, Any] = {}
    error: str | None = None
    passed = False

    now = datetime.now(tz=timezone.utc)
    long_retain = now + timedelta(days=365)

    try:
        put_resp = client.put_object(
            Bucket=bucket,
            Key=key,
            Body=original,
            ObjectLockMode="COMPLIANCE",
            ObjectLockRetainUntilDate=long_retain,
        )
        original_etag = put_resp.get("ETag", "")
        request_sample = {"operation": "put_object", "key": key, "mode": "COMPLIANCE"}

        # Attempt overwrite
        try:
            client.put_object(Bucket=bucket, Key=key, Body=overwrite)
            error = "Overwrite succeeded on COMPLIANCE-locked object (expected blocked)"
            passed = False
        except Exception as overwrite_exc:
            exc_str = str(overwrite_exc)
            blocked_codes = ["AccessDenied", "403", "ObjectLocked", "InvalidObjectState"]
            if any(c in exc_str for c in blocked_codes):
                passed = True
                response_sample = {"overwrite_error": exc_str[:200], "original_etag": original_etag}
            else:
                error = f"Unexpected overwrite error: {exc_str[:200]}"
    except Exception as exc:
        error = f"Setup failed: {exc}"

    return TestRecord(
        test_name="test_overwrite_in_place_rejected_for_locked_object",
        passed=passed,
        framework=framework,
        backend=backend,
        backend_version=backend_version,
        timestamp=timestamp,
        request_sample=request_sample,
        response_sample=response_sample,
        error=error,
    )
