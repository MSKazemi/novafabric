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
    # --- Signing block (FR-13). Populated by nova_worm_conformance.signing. ---
    # ``novaseal_signature`` holds a REAL signature or None — never a bare hash.
    signing_status: str = "unsigned"  # "signed" | "unsigned"
    content_sha256: str | None = None  # honest SHA-256 digest of ``signable_bytes()``
    signing_method: str | None = None  # e.g. "novaseal-ecdsa-p256" when truly signed
    signing_detail: str | None = None  # human-readable note (e.g. why it is unsigned)
    signing_cert: str | None = None  # base64 DER X.509 cert of the signer, when signed
    novaseal_signature: str | None = None  # base64 real signature; None unless signed

    # Fields excluded from the signed payload (a report cannot sign itself).
    _SIGNING_FIELDS = (
        "signing_status",
        "content_sha256",
        "signing_method",
        "signing_detail",
        "signing_cert",
        "novaseal_signature",
    )

    @property
    def all_passed(self) -> bool:
        return self.failed == 0

    def body_dict(self) -> dict[str, Any]:
        """Report content excluding the signing block (the payload that gets signed)."""
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

    def signable_bytes(self) -> bytes:
        """Canonical bytes of the report body that a signature covers.

        Deterministic (sorted keys) and independent of the signing block, so a
        signature verifies against exactly what was signed.
        """
        return json.dumps(self.body_dict(), sort_keys=True, default=str).encode("utf-8")

    def to_dict(self) -> dict[str, Any]:
        d = self.body_dict()
        d.update(
            {
                "signing_status": self.signing_status,
                "content_sha256": self.content_sha256,
                "signing_method": self.signing_method,
                "signing_detail": self.signing_detail,
                "signing_cert": self.signing_cert,
                "novaseal_signature": self.novaseal_signature,
            }
        )
        return d

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
