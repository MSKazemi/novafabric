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
"""Backend router and in-memory adapter for the Object Capsule Store.

``BackendRouter`` is a thin dispatch layer that resolves the correct
``WormAdapter`` subclass for a given backend tag.

``InMemoryWormAdapter`` is a non-WORM, in-memory adapter for unit tests that
must not touch the network.  It is NOT suitable for regulated deployments.
"""

from __future__ import annotations

import logging
from typing import Any

from novafabric.object_capsule_store.worm.base import (
    ConditionalPutConflict,
    WormAdapter,
    WormPutResult,
)

log = logging.getLogger(__name__)

_BACKENDS = ("s3", "minio", "ceph_rgw", "azure_blob", "local")


def make_adapter(backend: str, **kwargs: Any) -> WormAdapter:
    """Factory that returns the correct ``WormAdapter`` for *backend*.

    Args:
        backend: One of ``"s3"``, ``"minio"``, ``"ceph_rgw"``, ``"azure_blob"``,
                 ``"local"`` (in-memory, test-only).
        **kwargs: Forwarded to the adapter constructor.

    Returns:
        A concrete ``WormAdapter`` instance.

    Raises:
        ValueError: for unknown backend tags.
    """
    if backend == "s3":
        from novafabric.object_capsule_store.worm.s3 import S3WormAdapter

        return S3WormAdapter(**kwargs)
    if backend == "minio":
        from novafabric.object_capsule_store.worm.minio import MinioWormAdapter

        return MinioWormAdapter(**kwargs)
    if backend == "ceph_rgw":
        from novafabric.object_capsule_store.worm.ceph import CephWormAdapter

        return CephWormAdapter(**kwargs)
    if backend == "azure_blob":
        from novafabric.object_capsule_store.worm.azure import AzureWormAdapter

        return AzureWormAdapter(**kwargs)
    if backend == "local":
        return InMemoryWormAdapter()
    raise ValueError(
        f"Unknown backend {backend!r}. Valid choices: {', '.join(_BACKENDS)}"
    )


class InMemoryWormAdapter(WormAdapter):
    """Non-WORM, in-memory adapter for unit tests.

    Stores all objects in a plain ``dict``.  Simulates conditional-PUT
    semantics for chain-log objects.  WORM locking is not enforced.

    NOT suitable for regulated deployments — dev/test only.
    """

    def __init__(self) -> None:
        self._store: dict[str, bytes] = {}

    def put_object(
        self,
        key: str,
        data: bytes,
        sha256_hex: str,
        retention_days: int,
        content_type: str = "application/octet-stream",
    ) -> WormPutResult:
        import time

        from novafabric.object_capsule_store.cas import compute_sha256

        computed = compute_sha256(data)
        if computed != sha256_hex:
            from novafabric.object_capsule_store.exceptions import CASMismatchError

            raise CASMismatchError(key=key, expected=sha256_hex, observed=computed)
        self._store[key] = data
        return WormPutResult(
            key=key,
            confirmation_token=f"etag-{sha256_hex[:8]}",
            locked_until_ms=int(time.time() * 1000) + retention_days * 86_400_000,
        )

    def apply_identical(
        self,
        key_a: str,
        data_a: bytes,
        sha256_a: str,
        key_b: str,
        data_b: bytes,
        sha256_b: str,
        retention_days: int,
    ) -> tuple[WormPutResult, WormPutResult]:
        result_a = self.put_object(key_a, data_a, sha256_a, retention_days)
        result_b = self.put_object(
            key_b, data_b, sha256_b, retention_days, content_type="application/json"
        )
        return result_a, result_b

    def get_object(self, key: str) -> bytes:
        try:
            return self._store[key]
        except KeyError:
            raise FileNotFoundError(f"Key not found: {key!r}")

    def put_log_object(self, key: str, data: bytes) -> str:
        self._store[key] = data
        return f"etag-log-{key[-8:]}"

    def put_log_object_if_absent(self, key: str, data: bytes) -> str:
        if key in self._store:
            raise ConditionalPutConflict(key)
        self._store[key] = data
        return f"etag-log-{key[-8:]}"

    def list_objects(self, prefix: str) -> list[str]:
        return sorted(k for k in self._store if k.startswith(prefix))

    def delete_object(self, key: str) -> None:
        self._store.pop(key, None)

    def object_exists(self, key: str) -> bool:
        return key in self._store

    # Convenience for tests
    def all_keys(self) -> list[str]:
        return sorted(self._store.keys())
