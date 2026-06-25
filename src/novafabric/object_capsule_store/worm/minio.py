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
"""MinIO Erasure Set WORM adapter (S3-compatible, Object Lock COMPLIANCE).

MinIO server (AGPL-3.0) is a separate operator-managed service — never linked.
This adapter communicates with MinIO exclusively over the network via the S3
API (boto3, Apache-2.0).  See ADR-0024 for the network-boundary exception.

Requires: ``pip install novafabric[worm-s3]``  (boto3 >=1.35, Apache-2.0)
"""

from __future__ import annotations

from typing import Any

from novafabric.object_capsule_store.worm.s3 import S3WormAdapter


class MinioWormAdapter(S3WormAdapter):
    """MinIO-specific S3-compatible WORM adapter.

    Identical to ``S3WormAdapter`` at the API level; distinguished for
    diagnostic and test-suite purposes (WORM conformance suite backend tag).

    MinIO uses the same S3 Object Lock API.  The ``endpoint_url`` parameter
    points to the MinIO server, e.g. ``http://localhost:9000``.
    """

    def __init__(
        self,
        bucket: str,
        endpoint_url: str,
        client: Any | None = None,
    ) -> None:
        super().__init__(bucket=bucket, client=client, endpoint_url=endpoint_url)
        self._endpoint_url = endpoint_url

    @property
    def backend_tag(self) -> str:
        return "minio"
