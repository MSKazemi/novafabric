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
"""Test 10: Conditional PUT (If-None-Match: *) returns 412 on existing key."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from nova_worm_conformance.report import TestRecord


def run(client: Any, bucket: str, framework: str, backend: str, backend_version: str) -> TestRecord:
    """Verify If-None-Match: * returns 412 PreconditionFailed when key exists."""
    import uuid
    key = f"worm-test/{uuid.uuid4()}/conditional-put.bin"
    data = b"first write"
    timestamp = datetime.now(tz=timezone.utc).isoformat()
    request_sample: dict[str, Any] = {}
    response_sample: dict[str, Any] = {}
    error: str | None = None
    passed = False

    try:
        # First write (no condition)
        client.put_object(Bucket=bucket, Key=key, Body=data)
        request_sample = {"operation": "put_object (unconditional)", "key": key}

        # Second write with If-None-Match: * → should return 412
        try:
            client.put_object(Bucket=bucket, Key=key, Body=b"second write", IfNoneMatch="*")
            error = "Conditional PUT succeeded (expected 412)"
            passed = False
        except Exception as cond_exc:
            exc_str = str(cond_exc)
            if any(c in exc_str for c in ["PreconditionFailed", "412", "ConditionalRequestFailed"]):
                passed = True
                response_sample = {"conditional_error": exc_str[:200]}
            else:
                error = f"Unexpected conditional error: {exc_str[:200]}"

    except Exception as exc:
        error = f"Test failed: {exc}"

    return TestRecord(
        test_name="test_conditional_put_returns_412_on_existing_key",
        passed=passed,
        framework=framework,
        backend=backend,
        backend_version=backend_version,
        timestamp=timestamp,
        request_sample=request_sample,
        response_sample=response_sample,
        error=error,
    )
