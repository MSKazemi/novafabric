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
"""Test 2: Retention period cannot be shortened on COMPLIANCE-locked object."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from nova_worm_conformance.report import TestRecord


def run(client: Any, bucket: str, framework: str, backend: str, backend_version: str) -> TestRecord:
    """Attempt to shorten the retention of a COMPLIANCE-locked object → expect 403."""
    import uuid
    key = f"worm-test/{uuid.uuid4()}/retention-short.bin"
    data = b"locked for a long time"
    timestamp = datetime.now(tz=timezone.utc).isoformat()
    request_sample: dict[str, Any] = {}
    response_sample: dict[str, Any] = {}
    error: str | None = None
    passed = False

    now = datetime.now(tz=timezone.utc)
    long_retain = now + timedelta(days=365)
    short_retain = now + timedelta(days=1)

    try:
        put_resp = client.put_object(
            Bucket=bucket,
            Key=key,
            Body=data,
            ObjectLockMode="COMPLIANCE",
            ObjectLockRetainUntilDate=long_retain,
        )
        request_sample = {
            "operation": "put_object",
            "key": key,
            "retain_until": long_retain.isoformat(),
        }
        response_sample = {"ETag": put_resp.get("ETag", "")}

        # Attempt to shorten retention
        try:
            client.put_object_retention(
                Bucket=bucket,
                Key=key,
                Retention={"Mode": "COMPLIANCE", "RetainUntilDate": short_retain},
            )
            error = "Retention shortening succeeded (expected AccessDenied)"
            passed = False
        except Exception as short_exc:
            exc_str = str(short_exc)
            blocked_codes = ["AccessDenied", "403", "InvalidRequest", "ObjectLocked"]
            if any(code in exc_str for code in blocked_codes):
                passed = True
                response_sample["shorten_error"] = exc_str[:200]
            else:
                error = f"Unexpected error: {exc_str[:200]}"
    except Exception as exc:
        error = f"Setup failed: {exc}"

    return TestRecord(
        test_name="test_retention_cannot_be_shortened",
        passed=passed,
        framework=framework,
        backend=backend,
        backend_version=backend_version,
        timestamp=timestamp,
        request_sample=request_sample,
        response_sample=response_sample,
        error=error,
    )
