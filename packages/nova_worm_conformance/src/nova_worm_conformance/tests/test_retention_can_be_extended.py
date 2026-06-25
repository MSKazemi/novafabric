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
"""Test 3: Retention period can be extended on COMPLIANCE-locked object."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from nova_worm_conformance.report import TestRecord


def run(client: Any, bucket: str, framework: str, backend: str, backend_version: str) -> TestRecord:
    """Extend the retention of a COMPLIANCE-locked object → expect success."""
    import uuid
    key = f"worm-test/{uuid.uuid4()}/retention-extend.bin"
    data = b"extendable retention object"
    timestamp = datetime.now(tz=timezone.utc).isoformat()
    request_sample: dict[str, Any] = {}
    response_sample: dict[str, Any] = {}
    error: str | None = None
    passed = False

    now = datetime.now(tz=timezone.utc)
    short_retain = now + timedelta(days=1)
    long_retain = now + timedelta(days=365)

    try:
        client.put_object(
            Bucket=bucket,
            Key=key,
            Body=data,
            ObjectLockMode="COMPLIANCE",
            ObjectLockRetainUntilDate=short_retain,
        )
        request_sample = {"operation": "put_object", "retain_until": short_retain.isoformat()}

        # Extend retention
        client.put_object_retention(
            Bucket=bucket,
            Key=key,
            Retention={"Mode": "COMPLIANCE", "RetainUntilDate": long_retain},
        )
        # Verify new retention
        head = client.head_object(Bucket=bucket, Key=key)
        actual_retain = head.get("ObjectLockRetainUntilDate")
        if actual_retain and actual_retain >= long_retain - timedelta(seconds=5):
            passed = True
            response_sample = {"new_retain_until": str(actual_retain)}
        else:
            error = f"Retention extension not reflected in head: {actual_retain}"
    except Exception as exc:
        error = f"Test failed: {exc}"

    return TestRecord(
        test_name="test_retention_can_be_extended",
        passed=passed,
        framework=framework,
        backend=backend,
        backend_version=backend_version,
        timestamp=timestamp,
        request_sample=request_sample,
        response_sample=response_sample,
        error=error,
    )
