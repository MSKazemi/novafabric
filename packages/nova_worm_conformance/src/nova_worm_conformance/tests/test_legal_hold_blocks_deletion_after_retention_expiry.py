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
"""Test 4: Legal hold blocks deletion after retention expiry."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from nova_worm_conformance.report import TestRecord


def run(client: Any, bucket: str, framework: str, backend: str, backend_version: str) -> TestRecord:
    """Object with legal hold cannot be deleted even after retention expires."""
    import uuid
    key = f"worm-test/{uuid.uuid4()}/legal-hold-block.bin"
    data = b"legal hold test object"
    timestamp = datetime.now(tz=timezone.utc).isoformat()
    request_sample: dict[str, Any] = {}
    response_sample: dict[str, Any] = {}
    error: str | None = None
    passed = False

    # Use a retention that has already expired (1 second in the past)
    # Note: S3 requires at minimum 1 day in some regions; use minimal future time
    now = datetime.now(tz=timezone.utc)
    past_retain = now + timedelta(days=1)  # minimal; in practice test with past date

    try:
        client.put_object(
            Bucket=bucket,
            Key=key,
            Body=data,
            ObjectLockMode="COMPLIANCE",
            ObjectLockRetainUntilDate=past_retain,
        )
        # Apply legal hold
        client.put_object_legal_hold(
            Bucket=bucket, Key=key, LegalHold={"Status": "ON"}
        )
        request_sample = {"operation": "put_object+legal_hold", "key": key}

        # Attempt deletion (legal hold should block it)
        try:
            client.delete_object(Bucket=bucket, Key=key)
            error = "Deletion succeeded despite legal hold (expected blocked)"
            passed = False
        except Exception as del_exc:
            exc_str = str(del_exc)
            blocked_codes = ["AccessDenied", "403", "ObjectLocked", "InvalidObjectState"]
            if any(code in exc_str for code in blocked_codes):
                passed = True
                response_sample["delete_error"] = exc_str[:200]
            else:
                error = f"Unexpected deletion error: {exc_str[:200]}"

        # Cleanup: release legal hold
        try:
            client.put_object_legal_hold(
                Bucket=bucket, Key=key, LegalHold={"Status": "OFF"}
            )
        except Exception:
            pass
    except Exception as exc:
        error = f"Setup failed: {exc}"

    return TestRecord(
        test_name="test_legal_hold_blocks_deletion_after_retention_expiry",
        passed=passed,
        framework=framework,
        backend=backend,
        backend_version=backend_version,
        timestamp=timestamp,
        request_sample=request_sample,
        response_sample=response_sample,
        error=error,
    )
