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
"""Test 1: Root cannot delete a COMPLIANCE-mode locked object."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from nova_worm_conformance.report import TestRecord


def run(
    client: Any, bucket: str, framework: str, backend: str, backend_version: str
) -> TestRecord:
    """Attempt to delete a COMPLIANCE-locked object → expect 403/AccessDenied."""
    import uuid
    key = f"worm-test/{uuid.uuid4()}/locked.bin"
    data = b"compliance locked object"
    timestamp = datetime.now(tz=timezone.utc).isoformat()
    request_sample: dict[str, Any] = {}
    response_sample: dict[str, Any] = {}
    error: str | None = None
    passed = False

    try:
        # Upload with 1-day COMPLIANCE lock
        put_resp = client.put_object(
            Bucket=bucket,
            Key=key,
            Body=data,
            ObjectLockMode="COMPLIANCE",
            ObjectLockRetainUntilDate=datetime.now(tz=timezone.utc).replace(
                year=datetime.now(tz=timezone.utc).year + 1
            ),
        )
        request_sample = {"operation": "put_object", "key": key, "lock_mode": "COMPLIANCE"}
        response_sample = {"ETag": put_resp.get("ETag", "")}

        # Attempt to delete
        try:
            client.delete_object(Bucket=bucket, Key=key)
            # If we get here, deletion was NOT blocked — test fails
            error = "Deletion succeeded on COMPLIANCE-locked object (expected 403)"
            passed = False
        except Exception as del_exc:
            exc_str = str(del_exc)
            blocked_codes = ["AccessDenied", "403", "ObjectLocked", "InvalidObjectState"]
            if any(code in exc_str for code in blocked_codes):
                passed = True
                response_sample["delete_error"] = exc_str[:200]
            else:
                error = f"Unexpected deletion error: {exc_str[:200]}"
    except Exception as exc:
        error = f"Setup failed: {exc}"

    return TestRecord(
        test_name="test_root_cannot_delete_locked_object",
        passed=passed,
        framework=framework,
        backend=backend,
        backend_version=backend_version,
        timestamp=timestamp,
        request_sample=request_sample,
        response_sample=response_sample,
        error=error,
    )
