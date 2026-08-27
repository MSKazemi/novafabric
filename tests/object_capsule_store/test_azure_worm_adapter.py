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
"""Contract tests for the object-capsule-store Azure Blob WORM adapter.

These deliberately do NOT use a bare ``MagicMock``: a MagicMock accepts any
argument of any type, which is precisely why two defects survived here until
the adapter was first run against a live storage account (2026-08-27):

1. ``put_object`` passed ``content_md5`` as base64 *text*.  The generated SDK
   serializes that header as "bytearray" and base64-encodes it itself, so every
   call raised ``TypeError: blob_content_md5 must be type bytearray`` — the
   adapter could never store a capsule.
2. ``upload_blob`` returns a **dict**, so ``getattr(resp, "version_id", "")``
   always missed and the WORM confirmation token was silently always "".

The fakes below enforce the two real SDK behaviours a MagicMock erases.
"""

from __future__ import annotations

import hashlib
from typing import Any

import pytest

from novafabric.object_capsule_store.worm.azure import AzureWormAdapter
from novafabric.object_capsule_store.worm.base import ConditionalPutConflict

# Real version id shape returned by the service (observed live 2026-08-27).
VERSION_ID = "2026-08-27T15:58:40.7869181Z"


class _StrictBlobClient:
    """Blob client that mimics the two SDK behaviours MagicMock hides."""

    def __init__(self, existing: set[str], key: str) -> None:
        self._existing = existing
        self._key = key
        self.last_content_settings: Any = None

    def upload_blob(self, data: bytes, **kwargs: Any) -> dict[str, Any]:
        settings = kwargs.get("content_settings")
        self.last_content_settings = settings
        md5 = getattr(settings, "content_md5", None)
        # The generated SDK layer base64-encodes this header itself and rejects
        # anything that is not bytes-like.
        if md5 is not None and not isinstance(md5, (bytes, bytearray)):
            raise TypeError("blob_content_md5 must be type bytearray.")
        if not kwargs.get("overwrite", False) and self._key in self._existing:
            raise RuntimeError(
                "The specified blob already exists.\nErrorCode:BlobAlreadyExists"
            )
        self._existing.add(self._key)
        # upload_blob returns a dict, never an object.
        return {"version_id": VERSION_ID, "etag": '"etag1"'}


class _StrictContainerClient:
    def __init__(self) -> None:
        self.existing: set[str] = set()

    def get_blob_client(self, key: str) -> _StrictBlobClient:
        return _StrictBlobClient(self.existing, key)


@pytest.fixture()
def adapter() -> AzureWormAdapter:
    pytest.importorskip("azure.storage.blob")
    return AzureWormAdapter(container_name="c", client=_StrictContainerClient())


def test_put_object_sends_raw_md5_digest_not_base64() -> None:
    """Regression: base64 text here made every capsule write raise TypeError."""
    pytest.importorskip("azure.storage.blob")
    container = _StrictContainerClient()
    blob = _StrictBlobClient(container.existing, "k/cap.json")
    container.get_blob_client = lambda _key: blob  # type: ignore[method-assign]
    adapter = AzureWormAdapter(container_name="c", client=container)

    data = b'{"capsule":"v1"}'
    adapter.put_object(
        "k/cap.json", data, hashlib.sha256(data).hexdigest(), retention_days=7
    )

    sent = blob.last_content_settings.content_md5
    assert isinstance(sent, (bytes, bytearray)), "content_md5 must be bytes-like"
    assert sent == hashlib.md5(data).digest()  # noqa: S324 — Azure requires MD5


def test_put_object_returns_version_id_from_dict_response(
    adapter: AzureWormAdapter,
) -> None:
    """Regression: getattr() on the dict response always yielded ""."""
    data = b"payload"
    result = adapter.put_object(
        "k/v.json", data, hashlib.sha256(data).hexdigest(), retention_days=7
    )
    assert result.confirmation_token == VERSION_ID


def test_put_log_object_returns_version_id(adapter: AzureWormAdapter) -> None:
    assert adapter.put_log_object("k/log.json", b"{}") == VERSION_ID


def test_put_log_object_if_absent_returns_version_id(
    adapter: AzureWormAdapter,
) -> None:
    assert adapter.put_log_object_if_absent("k/occ.json", b"{}") == VERSION_ID


def test_put_log_object_if_absent_raises_conditional_put_conflict(
    adapter: AzureWormAdapter,
) -> None:
    """AC-2: conditional PUT must surface as ConditionalPutConflict.

    Confirmed against a live container 2026-08-27: the SDK raises
    ``ResourceExistsError`` whose str() carries ``ErrorCode:BlobAlreadyExists``.
    """
    adapter.put_log_object_if_absent("k/dup.json", b'{"v":1}')
    with pytest.raises(ConditionalPutConflict, match="k/dup.json"):
        adapter.put_log_object_if_absent("k/dup.json", b'{"v":2}')
