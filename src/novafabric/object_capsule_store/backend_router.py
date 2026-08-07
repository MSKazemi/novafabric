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

Opt-in envelope encryption (ADR-0185, experimental): when
``NOVA_OBJECT_STORE_ENCRYPTION=1`` and ``NOVA_OBJECT_STORE_KEK_PATH`` point at
a local 256-bit KEK file, ``make_adapter`` wraps the chosen backend in an
``EncryptingAdapter`` so every capsule payload is envelope-encrypted before
the WORM write.  Absent that configuration, behavior is byte-for-byte
unchanged — encryption is never the default.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from novafabric.object_capsule_store.worm.base import (
    ConditionalPutConflict,
    WormAdapter,
    WormPutResult,
)

log = logging.getLogger(__name__)

_BACKENDS = ("s3", "minio", "ceph_rgw", "azure_blob", "local")

# Opt-in envelope-encryption configuration (ADR-0185, experimental).
ENV_ENCRYPTION = "NOVA_OBJECT_STORE_ENCRYPTION"
ENV_KEK_PATH = "NOVA_OBJECT_STORE_KEK_PATH"
ENV_TENANT_KEK_DIR = "NOVA_OBJECT_STORE_TENANT_KEK_DIR"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _maybe_wrap_encryption(adapter: WormAdapter) -> WormAdapter:
    """Wrap *adapter* in an ``EncryptingAdapter`` when opted in via env (ADR-0185).

    Opt-in requires BOTH ``NOVA_OBJECT_STORE_ENCRYPTION`` truthy ("1"/"true"/
    "yes"/"on") AND ``NOVA_OBJECT_STORE_KEK_PATH`` naming a local 256-bit KEK
    file (32 raw bytes or 64 hex chars).  A truthy flag without a KEK path is a
    misconfiguration and fails closed — silently writing plaintext when the
    operator asked for encryption would violate ADR-0185.

    Returns *adapter* unchanged when the flag is absent/falsy (the default:
    zero behavior change).
    """
    flag = os.environ.get(ENV_ENCRYPTION, "").strip().lower()
    if flag not in _TRUTHY:
        return adapter
    kek_value = os.environ.get(ENV_KEK_PATH, "").strip()
    if not kek_value:
        raise ValueError(
            f"{ENV_ENCRYPTION} is set but {ENV_KEK_PATH} is not: envelope encryption "
            "(ADR-0185) requires a local 256-bit KEK file (32 raw bytes or 64 hex "
            "chars). Refusing to fall back to plaintext writes."
        )
    from pathlib import Path

    from novafabric.object_capsule_store.encryption_wrapper import EncryptingAdapter
    from novafabric.trust.novaseal.signing_backend import LocalSigningBackend

    kek_path = Path(kek_value)
    # key_path/cert_path are unused by the wrap capability; only kek_path matters.
    backend = LocalSigningBackend(kek_path, kek_path, kek_path=kek_path)

    # ADR-0243 slice 1: optional per-tenant KEKs layered over the default.
    tenant_keys = None
    tenant_dir_value = os.environ.get(ENV_TENANT_KEK_DIR, "").strip()
    if tenant_dir_value:
        from novafabric.trust.tenant_keys import TenantKeyRegistry

        tenant_dir = Path(tenant_dir_value)
        if not tenant_dir.is_dir():
            raise ValueError(
                f"{ENV_TENANT_KEK_DIR} is set but {tenant_dir} is not a directory: "
                "per-tenant envelope encryption (ADR-0243) requires an existing "
                "directory of <tenant>.kek files. Refusing to start misconfigured."
            )
        tenant_keys = TenantKeyRegistry(backend, tenant_dir)

    log.info(
        "object-capsule-store envelope encryption enabled (ADR-0185, experimental): "
        "wrapping %s with EncryptingAdapter (local KEK%s)",
        type(adapter).__name__,
        ", per-tenant KEK dir" if tenant_keys is not None else "",
    )
    return EncryptingAdapter(adapter, backend, tenant_keys=tenant_keys)


def make_adapter(backend: str, **kwargs: Any) -> WormAdapter:
    """Factory that returns the correct ``WormAdapter`` for *backend*.

    When envelope encryption is opted in via ``NOVA_OBJECT_STORE_ENCRYPTION=1``
    + ``NOVA_OBJECT_STORE_KEK_PATH`` (ADR-0185, experimental), the returned
    adapter is wrapped in an ``EncryptingAdapter``; otherwise the bare adapter
    is returned unchanged.

    Args:
        backend: One of ``"s3"``, ``"minio"``, ``"ceph_rgw"``, ``"azure_blob"``,
                 ``"local"`` (in-memory, test-only).
        **kwargs: Forwarded to the adapter constructor.

    Returns:
        A concrete ``WormAdapter`` instance.

    Raises:
        ValueError: for unknown backend tags, or when encryption is enabled
                    without a KEK path.
    """
    adapter: WormAdapter
    if backend == "s3":
        from novafabric.object_capsule_store.worm.s3 import S3WormAdapter

        adapter = S3WormAdapter(**kwargs)
    elif backend == "minio":
        from novafabric.object_capsule_store.worm.minio import MinioWormAdapter

        adapter = MinioWormAdapter(**kwargs)
    elif backend == "ceph_rgw":
        from novafabric.object_capsule_store.worm.ceph import CephWormAdapter

        adapter = CephWormAdapter(**kwargs)
    elif backend == "azure_blob":
        from novafabric.object_capsule_store.worm.azure import AzureWormAdapter

        adapter = AzureWormAdapter(**kwargs)
    elif backend == "local":
        adapter = InMemoryWormAdapter()
    else:
        raise ValueError(
            f"Unknown backend {backend!r}. Valid choices: {', '.join(_BACKENDS)}"
        )
    return _maybe_wrap_encryption(adapter)


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
