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
"""Test 7: Compliance mode cannot be disabled after bucket creation."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from nova_worm_conformance.report import TestRecord


def run(client: Any, bucket: str, framework: str, backend: str, backend_version: str) -> TestRecord:
    """Attempt to disable Object Lock on an existing WORM bucket → expect error."""
    timestamp = datetime.now(tz=timezone.utc).isoformat()
    request_sample: dict[str, Any] = {}
    response_sample: dict[str, Any] = {}
    error: str | None = None
    passed = False

    try:
        # Check current Object Lock configuration
        current = client.get_object_lock_configuration(Bucket=bucket)
        request_sample = {"operation": "get_object_lock_configuration", "bucket": bucket}
        response_sample = {"current_config": str(current.get("ObjectLockConfiguration", {}))}

        # Attempt to disable Object Lock
        try:
            client.put_object_lock_configuration(
                Bucket=bucket,
                ObjectLockConfiguration={"ObjectLockEnabled": "Disabled"},
            )
            error = "Object Lock disable succeeded (expected error)"
            passed = False
        except Exception as disable_exc:
            exc_str = str(disable_exc)
            blocked_codes = [
                "AccessDenied", "InvalidArgument", "403", "MalformedXML", "InvalidRequest",
            ]
            if any(c in exc_str for c in blocked_codes):
                passed = True
                response_sample["disable_error"] = exc_str[:200]
            else:
                error = f"Unexpected disable error: {exc_str[:200]}"

    except Exception as exc:
        error = f"Setup failed: {exc}"

    return TestRecord(
        test_name="test_compliance_mode_cannot_be_disabled_after_bucket_creation",
        passed=passed,
        framework=framework,
        backend=backend,
        backend_version=backend_version,
        timestamp=timestamp,
        request_sample=request_sample,
        response_sample=response_sample,
        error=error,
    )
