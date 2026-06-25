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
"""Test 8: Governance bypass header does not apply to COMPLIANCE-mode objects."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from nova_worm_conformance.report import TestRecord


def run(client: Any, bucket: str, framework: str, backend: str, backend_version: str) -> TestRecord:
    """x-amz-bypass-governance-retention header must NOT bypass COMPLIANCE mode."""
    import uuid
    key = f"worm-test/{uuid.uuid4()}/governance-bypass.bin"
    data = b"compliance mode no bypass"
    timestamp = datetime.now(tz=timezone.utc).isoformat()
    request_sample: dict[str, Any] = {}
    response_sample: dict[str, Any] = {}
    error: str | None = None
    passed = False

    now = datetime.now(tz=timezone.utc)
    long_retain = now + timedelta(days=365)

    try:
        client.put_object(
            Bucket=bucket,
            Key=key,
            Body=data,
            ObjectLockMode="COMPLIANCE",
            ObjectLockRetainUntilDate=long_retain,
        )
        request_sample = {"operation": "put_object", "mode": "COMPLIANCE", "key": key}

        # Attempt delete with governance bypass header
        try:
            client.delete_object(
                Bucket=bucket,
                Key=key,
                BypassGovernanceRetention=True,
            )
            error = "Delete with governance bypass succeeded on COMPLIANCE object"
            passed = False
        except Exception as del_exc:
            exc_str = str(del_exc)
            blocked_codes = ["AccessDenied", "403", "ObjectLocked", "InvalidObjectState"]
            if any(c in exc_str for c in blocked_codes):
                passed = True
                response_sample["bypass_delete_error"] = exc_str[:200]
            else:
                error = f"Unexpected error: {exc_str[:200]}"
    except Exception as exc:
        error = f"Setup failed: {exc}"

    return TestRecord(
        test_name="test_governance_bypass_does_not_apply_in_compliance_mode",
        passed=passed,
        framework=framework,
        backend=backend,
        backend_version=backend_version,
        timestamp=timestamp,
        request_sample=request_sample,
        response_sample=response_sample,
        error=error,
    )
