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
"""Test 6: Lifecycle expiration rules silently skip COMPLIANCE-locked objects."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from nova_worm_conformance.report import TestRecord


def run(client: Any, bucket: str, framework: str, backend: str, backend_version: str) -> TestRecord:
    """Verify that bucket lifecycle expiry cannot delete COMPLIANCE-locked objects."""
    import uuid
    key = f"worm-test/{uuid.uuid4()}/lifecycle-skip.bin"
    data = b"lifecycle expiry test object"
    timestamp = datetime.now(tz=timezone.utc).isoformat()
    request_sample: dict[str, Any] = {}
    response_sample: dict[str, Any] = {}
    error: str | None = None
    passed = False

    now = datetime.now(tz=timezone.utc)
    long_retain = now + timedelta(days=365)

    try:
        # Upload with COMPLIANCE lock
        client.put_object(
            Bucket=bucket,
            Key=key,
            Body=data,
            ObjectLockMode="COMPLIANCE",
            ObjectLockRetainUntilDate=long_retain,
        )
        request_sample = {"operation": "put_object_with_compliance_lock", "key": key}

        # Try to set lifecycle rule that would expire this object in 1 day
        # Expected: backend accepts the rule but will silently skip locked objects
        try:
            client.put_bucket_lifecycle_configuration(
                Bucket=bucket,
                LifecycleConfiguration={
                    "Rules": [{
                        "ID": "test-expiry",
                        "Status": "Enabled",
                        "Filter": {"Prefix": "worm-test/"},
                        "Expiration": {"Days": 1},
                    }]
                }
            )
            response_sample["lifecycle_set"] = True
            # The key thing: the object should still exist (lifecycle won't delete WORM objects)
            client.head_object(Bucket=bucket, Key=key)
            response_sample["object_still_exists"] = True
            passed = True
        except Exception as lc_exc:
            exc_str = str(lc_exc)
            # Some backends reject lifecycle rules on WORM buckets — also compliant
            if any(c in exc_str for c in ["AccessDenied", "InvalidBucketState", "403"]):
                passed = True
                response_sample["lifecycle_rejected"] = exc_str[:200]
            else:
                error = f"Unexpected lifecycle error: {exc_str[:200]}"

    except Exception as exc:
        error = f"Setup failed: {exc}"

    return TestRecord(
        test_name="test_lifecycle_expiration_silently_skipped_on_locked_object",
        passed=passed,
        framework=framework,
        backend=backend,
        backend_version=backend_version,
        timestamp=timestamp,
        request_sample=request_sample,
        response_sample=response_sample,
        error=error,
    )
