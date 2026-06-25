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
"""Ceph RGW Pacific+ WORM adapter (S3-compatible, Object Lock COMPLIANCE).

Ceph RGW exposes an S3-compatible API with Object Lock support since Pacific
(v16.2).  This adapter is identical to ``S3WormAdapter`` at the API surface;
it is a distinct class for diagnostic, test-suite, and COMPATIBILITY.md purposes.

Requires: ``pip install novafabric[worm-s3]``  (boto3 >=1.35, Apache-2.0)
"""

from __future__ import annotations

from typing import Any

from novafabric.object_capsule_store.worm.s3 import S3WormAdapter


class CephWormAdapter(S3WormAdapter):
    """Ceph RGW S3-compatible WORM adapter (Object Lock COMPLIANCE mode).

    ``endpoint_url`` should point to the Ceph RGW S3 endpoint,
    e.g. ``http://ceph-rgw.example.com:7480``.
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
        return "ceph_rgw"
