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
"""WormConformanceSuite — runs all 11 mandated test cases (FR-11, FR-15)."""

from __future__ import annotations

import importlib
import logging
from datetime import datetime, timezone
from typing import Any

from nova_worm_conformance.frameworks import get_test_cases
from nova_worm_conformance.report import ConformanceReport, TestRecord, build_report

log = logging.getLogger(__name__)

_TEST_MODULE_PREFIX = "nova_worm_conformance.tests."


class WormConformanceSuite:
    """Run WORM conformance tests against a backend client.

    Args:
        client:  boto3 S3 client (or Azure ContainerClient for azure_blob).
        backend: Backend tag (s3, minio, ceph_rgw, azure_blob).
        bucket:  Target bucket name.
        framework: Regulatory framework (sec-17a-4, mifid-ii, etc.).
        endpoint: Backend endpoint URL.
    """

    def __init__(
        self,
        client: Any,
        backend: str,
        bucket: str,
        framework: str,
        endpoint: str = "",
    ) -> None:
        self._client = client
        self._backend = backend
        self._bucket = bucket
        self._framework = framework
        self._endpoint = endpoint

    def _get_backend_version(self) -> str:
        """Attempt to retrieve backend server version."""
        try:
            resp = self._client.head_bucket(Bucket=self._bucket)
            headers = resp.get("ResponseMetadata", {}).get("HTTPHeaders", {})
            return str(headers.get("x-amz-server-version", "unknown"))
        except Exception:
            return "unknown"

    def run(self) -> ConformanceReport:
        """Execute all required test cases for the configured framework.

        Returns:
            ``ConformanceReport`` with per-test records.
        """
        started_at = datetime.now(tz=timezone.utc).isoformat()
        test_names = get_test_cases(self._framework)
        backend_version = self._get_backend_version()
        records: list[TestRecord] = []

        for test_name in test_names:
            module_name = _TEST_MODULE_PREFIX + test_name
            try:
                module = importlib.import_module(module_name)
                record = module.run(
                    client=self._client,
                    bucket=self._bucket,
                    framework=self._framework,
                    backend=self._backend,
                    backend_version=backend_version,
                )
            except Exception as exc:
                log.error("Test %s raised an unexpected error: %s", test_name, exc)
                record = TestRecord(
                    test_name=test_name,
                    passed=False,
                    framework=self._framework,
                    backend=self._backend,
                    backend_version=backend_version,
                    timestamp=datetime.now(tz=timezone.utc).isoformat(),
                    error=f"Internal test error: {exc}",
                )
            records.append(record)
            status = "PASS" if record.passed else "FAIL"
            log.info("%s %s", status, test_name)

        return build_report(
            backend=self._backend,
            endpoint=self._endpoint,
            bucket=self._bucket,
            framework=self._framework,
            records=records,
            started_at=started_at,
        )
