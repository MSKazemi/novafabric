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
"""Google Cloud Storage WORM adapter (per-object retention, LOCKED mode).

Requires: ``pip install novafabric[worm-gcs]``  (google-cloud-storage >=2.19, Apache-2.0)

OPERATOR REQUIREMENTS:
    The GCS bucket MUST be created with per-object retention enabled:

        gcloud storage buckets create gs://<bucket> \\
            --enable-per-object-retention \\
            --uniform-bucket-level-access

    Per-object retention (``objectRetention`` resource) is a GCS feature that
    applies WORM semantics to individual objects rather than the entire bucket.
    Unlike Bucket Lock (which is bucket-wide), per-object retention allows
    different retention periods per object — mapping cleanly to S3 Object Lock
    COMPLIANCE mode semantics.

    Attempting to delete or overwrite a LOCKED object before its retainUntilTime
    will return HTTP 403 from GCS.  This adapter does NOT suppress that error.

    Reference:
        https://cloud.google.com/storage/docs/object-lock
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from novafabric.object_capsule_store.exceptions import CASMismatchError
from novafabric.object_capsule_store.worm.base import (
    ConditionalPutConflict,
    WormAdapter,
    WormPutResult,
)

log = logging.getLogger(__name__)


class GcsWormAdapter(WormAdapter):
    """GCS WORM adapter using per-object retention (LOCKED mode).

    The bucket MUST have per-object retention enabled at creation time
    (``--enable-per-object-retention``).  See module docstring for details.

    Args:
        bucket_name:     GCS bucket name.
        client:          Optional pre-built ``google.cloud.storage.Client`` (for testing).
        project:         GCP project ID (used only when constructing a new client).
    """

    def __init__(
        self,
        bucket_name: str,
        client: Any | None = None,
        project: str | None = None,
    ) -> None:
        self._bucket_name = bucket_name
        if client is not None:
            self._client: Any = client
        else:
            from google.cloud import storage as gcs  # type: ignore[attr-defined]

            kwargs: dict[str, Any] = {}
            if project:
                kwargs["project"] = project
            self._client = gcs.Client(**kwargs)

    # -----------------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------------

    def _bucket(self) -> Any:
        return self._client.bucket(self._bucket_name)

    def _blob(self, key: str) -> Any:
        return self._bucket().blob(key)

    # -----------------------------------------------------------------------
    # WormAdapter implementation
    # -----------------------------------------------------------------------

    def put_object(
        self,
        key: str,
        data: bytes,
        sha256_hex: str,
        retention_days: int,
        content_type: str = "application/octet-stream",
    ) -> WormPutResult:
        """Upload *data* to *key* with per-object WORM retention (LOCKED mode).

        Sets ``objectRetention.mode = LOCKED`` and ``objectRetention.retainUntilTime``
        on the blob.  GCS will reject DELETE and overwrite requests until the
        retention period expires.

        Raises:
            CASMismatchError: if the GCS backend rejects the upload due to a
                checksum mismatch (HTTP 400).
        """
        locked_until = datetime.now(tz=timezone.utc) + timedelta(days=retention_days)
        blob = self._blob(key)
        try:
            blob.upload_from_string(data, content_type=content_type)
        except Exception as exc:
            exc_str = str(exc)
            if (
                "checksum" in exc_str.lower()
                or "hash" in exc_str.lower()
                or "400" in exc_str
            ):
                raise CASMismatchError(
                    key=key, expected=sha256_hex, observed="<backend_rejected>"
                ) from exc
            raise

        # Apply per-object retention (LOCKED mode)
        blob.retention.mode = "LOCKED"
        blob.retention.retain_until_time = locked_until
        blob.patch()

        generation: str = str(getattr(blob, "generation", "") or "")
        return WormPutResult(
            key=key,
            confirmation_token=generation,
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
        """Upload two objects with identical WORM policy (FR-07)."""
        result_a = self.put_object(key_a, data_a, sha256_a, retention_days)
        result_b = self.put_object(
            key_b, data_b, sha256_b, retention_days, content_type="application/json"
        )
        return result_a, result_b

    def get_object(self, key: str) -> bytes:
        """Download and return the raw bytes at *key*."""
        blob = self._blob(key)
        return blob.download_as_bytes()  # type: ignore[no-any-return]

    def put_log_object(self, key: str, data: bytes) -> str:
        """Upload a chain-log object without WORM retention (FR constraint).

        Returns the GCS generation number as the confirmation token.
        """
        blob = self._blob(key)
        blob.upload_from_string(data, content_type="application/json")
        return str(getattr(blob, "generation", "") or "")

    def put_log_object_if_absent(self, key: str, data: bytes) -> str:
        """Conditional-PUT: succeed only if *key* does not yet exist.

        Uses GCS ``if_generation_match=0`` precondition which maps to
        ``If-None-Match: *`` semantics.

        Raises:
            ConditionalPutConflict: if the key already exists (HTTP 412 equivalent).
        """
        # PreconditionFailed is part of google-cloud-core (always installed with
        # google-cloud-storage).  We catch it by class when available, and fall back
        # to string-matching when the google package is not installed (test environments
        # without worm-gcs extra).
        try:
            from google.api_core.exceptions import (
                PreconditionFailed,
            )
            _precondition_cls: type[Exception] | None = PreconditionFailed
        except ImportError:
            _precondition_cls = None

        blob = self._blob(key)
        try:
            blob.upload_from_string(
                data,
                content_type="application/json",
                if_generation_match=0,
            )
            return str(getattr(blob, "generation", "") or "")
        except Exception as exc:
            # Check by class first (when google.api_core is available)
            if _precondition_cls is not None and isinstance(exc, _precondition_cls):
                raise ConditionalPutConflict(key) from exc
            # Fall back to string matching for test mocks and environments without
            # the google-cloud-storage extra installed.
            exc_str = str(exc)
            if (
                "412" in exc_str
                or "conditionNotMet" in exc_str
                or "precondition" in exc_str.lower()
            ):
                raise ConditionalPutConflict(key) from exc
            raise

    def list_objects(self, prefix: str) -> list[str]:
        """List all object keys under *prefix* (sorted)."""
        blobs = self._client.list_blobs(self._bucket_name, prefix=prefix)
        return sorted(b.name for b in blobs)

    def delete_object(self, key: str) -> None:
        """Attempt to delete *key*.

        GCS will raise HTTP 403 if the object is under LOCKED retention.
        This method does NOT suppress that error — callers must catch it.
        """
        blob = self._blob(key)
        blob.delete()

    def object_exists(self, key: str) -> bool:
        """Return True if *key* exists in the bucket."""
        blob = self._blob(key)
        return blob.exists()  # type: ignore[no-any-return]
