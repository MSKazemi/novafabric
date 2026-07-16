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
"""Azure Blob Storage immutable WORM adapter.

Uses azure-storage-blob (MIT, >=12.23) — Tier A under ADR-0024.

Azure's immutability model uses time-based retention policies applied to
containers (or individual blobs in premium accounts).  This adapter targets
the container-level immutability policy which is the most common deployment
pattern and maps most cleanly to the S3 Object Lock COMPLIANCE semantics.

OPERATOR REQUIREMENT (gap-audit.md Domain 2 / BQ-013):
    Unlike S3 Object Lock (where per-object WORM is set on PUT), Azure Blob
    immutability is configured at the *container* level before any objects are
    uploaded.  Operators MUST create the container with a time-based immutability
    policy (``az storage container immutability-policy create``) and lock it
    (``az storage container immutability-policy lock``) before deploying this
    adapter in a regulated environment.  Uploading to a container without a
    locked immutability policy will NOT enforce WORM semantics even though this
    adapter sets the correct metadata headers.

    This is an Azure platform constraint, not an adapter defect.  See:
    https://docs.microsoft.com/en-us/azure/storage/blobs/immutable-storage-overview

Requires: ``pip install novafabric[worm-azure]``
"""

from __future__ import annotations

import base64
import hashlib
import logging
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import Any

from novafabric.object_capsule_store.exceptions import CASMismatchError
from novafabric.object_capsule_store.worm.base import (
    ConditionalPutConflict,
    WormAdapter,
    WormPutResult,
)

log = logging.getLogger(__name__)


class AzureWormAdapter(WormAdapter):
    """Azure Blob Storage WORM adapter.

    The container must have an immutable storage policy configured before use.
    This adapter sets ``x-ms-meta-sha256`` on every data blob for CAS
    verification (FR-15) since Azure Blob does not natively support SHA-256
    checksums at the API layer.

    Args:
        container_name:    Azure Blob container name.
        connection_string: Azure storage connection string.
        client:            Optional pre-built ``ContainerClient`` (for testing).
    """

    def __init__(
        self,
        container_name: str,
        connection_string: str = "",
        client: Any | None = None,
    ) -> None:
        self._container = container_name
        if client is not None:
            self._container_client: Any = client
        else:
            from azure.storage.blob import ContainerClient

            self._container_client = ContainerClient.from_connection_string(
                connection_string, container_name
            )

    def put_object(
        self,
        key: str,
        data: bytes,
        sha256_hex: str,
        retention_days: int,
        content_type: str = "application/octet-stream",
    ) -> WormPutResult:
        locked_until = datetime.now(tz=timezone.utc) + timedelta(days=retention_days)
        md5_b64 = base64.b64encode(hashlib.md5(data).digest()).decode("ascii")  # noqa: S324 — Azure requires MD5
        metadata = {"sha256": sha256_hex}
        blob_client = self._container_client.get_blob_client(key)
        try:
            resp = blob_client.upload_blob(
                data,
                overwrite=False,
                content_settings=self._content_settings(content_type, md5_b64),
                metadata=metadata,
            )
        except Exception as exc:
            exc_str = str(exc)
            if "Md5Mismatch" in exc_str or "InvalidMd5" in exc_str:
                raise CASMismatchError(
                    key=key, expected=sha256_hex, observed="<azure_md5_mismatch>"
                ) from exc
            raise
        version_id: str = getattr(resp, "version_id", "") or ""
        return WormPutResult(
            key=key,
            confirmation_token=version_id,
            locked_until_ms=int(locked_until.timestamp() * 1000),
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
        blob_client = self._container_client.get_blob_client(key)
        downloader = blob_client.download_blob()
        return downloader.readall()  # type: ignore[no-any-return]

    def put_log_object(self, key: str, data: bytes) -> str:
        blob_client = self._container_client.get_blob_client(key)
        resp = blob_client.upload_blob(
            data, overwrite=True, content_settings=self._content_settings("application/json")
        )
        return getattr(resp, "version_id", "") or ""

    def put_log_object_if_absent(self, key: str, data: bytes) -> str:
        blob_client = self._container_client.get_blob_client(key)
        try:
            resp = blob_client.upload_blob(
                data,
                overwrite=False,
                content_settings=self._content_settings("application/json"),
            )
            return getattr(resp, "version_id", "") or ""
        except Exception as exc:
            if "BlobAlreadyExists" in str(exc):
                raise ConditionalPutConflict(key) from exc
            raise

    def iter_objects(self, prefix: str) -> Iterator[str]:
        """Stream keys via the Azure list_blobs lazy iterator (ADR-0175).

        ``list_blobs`` returns an auto-paging ``ItemPaged`` iterator, so yielding
        from it keeps peak memory bounded regardless of namespace size.
        """
        for b in self._container_client.list_blobs(name_starts_with=prefix):
            yield b.name

    def list_objects(self, prefix: str) -> list[str]:
        return sorted(self.iter_objects(prefix))

    def delete_object(self, key: str) -> None:
        blob_client = self._container_client.get_blob_client(key)
        blob_client.delete_blob()

    def object_exists(self, key: str) -> bool:
        blob_client = self._container_client.get_blob_client(key)
        return blob_client.exists()  # type: ignore[no-any-return]

    @staticmethod
    def _content_settings(content_type: str, md5_b64: str = "") -> Any:
        from azure.storage.blob import ContentSettings  # noqa: PLC0415

        kwargs: dict[str, Any] = {"content_type": content_type}
        if md5_b64:
            kwargs["content_md5"] = md5_b64
        return ContentSettings(**kwargs)
