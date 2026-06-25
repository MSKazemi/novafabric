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
"""JSON attestation report writer for WORM conformance results (FR-10, FR-13)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class TestRecord:
    """Result of a single WORM conformance test case."""

    test_name: str
    passed: bool
    framework: str
    backend: str
    backend_version: str
    timestamp: str
    request_sample: dict[str, Any] = field(default_factory=dict)
    response_sample: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


@dataclass
class ConformanceReport:
    """Full WORM conformance test run report."""

    backend: str
    endpoint: str
    bucket: str
    framework: str
    started_at: str
    completed_at: str
    total: int
    passed: int
    failed: int
    records: list[TestRecord] = field(default_factory=list)
    novaseal_signature: str | None = None  # Populated when --sign is used (FR-13)

    @property
    def all_passed(self) -> bool:
        return self.failed == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "endpoint": self.endpoint,
            "bucket": self.bucket,
            "framework": self.framework,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "total": self.total,
            "passed": self.passed,
            "failed": self.failed,
            "all_passed": self.all_passed,
            "novaseal_signature": self.novaseal_signature,
            "records": [
                {
                    "test_name": r.test_name,
                    "passed": r.passed,
                    "framework": r.framework,
                    "backend": r.backend,
                    "backend_version": r.backend_version,
                    "timestamp": r.timestamp,
                    "request_sample": r.request_sample,
                    "response_sample": r.response_sample,
                    "error": r.error,
                }
                for r in self.records
            ],
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, default=str)

    def write(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            f.write(self.to_json())


def build_report(
    backend: str,
    endpoint: str,
    bucket: str,
    framework: str,
    records: list[TestRecord],
    started_at: str,
) -> ConformanceReport:
    """Assemble a ``ConformanceReport`` from test records."""
    passed = sum(1 for r in records if r.passed)
    failed = len(records) - passed
    completed_at = datetime.now(tz=timezone.utc).isoformat()
    return ConformanceReport(
        backend=backend,
        endpoint=endpoint,
        bucket=bucket,
        framework=framework,
        started_at=started_at,
        completed_at=completed_at,
        total=len(records),
        passed=passed,
        failed=failed,
        records=records,
    )
