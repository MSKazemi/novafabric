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
"""Abstract WormAdapter interface for the Object Capsule Store.

Implements the interface specified in ADR-0031 §1, extended with the
``apply_identical()`` method required by cap-002 FR-07 (identical WORM policy
on data + NovaSeal sibling via a single adapter call).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass


@dataclass
class WormPutResult:
    """Result of a WORM put operation."""

    key: str
    confirmation_token: str  # ETag (S3), versionId (Azure), row-id (local)
    locked_until_ms: int      # Unix epoch milliseconds (UTC)


class WormAdapter(ABC):
    """Abstract base class for WORM backend adapters (ADR-0031).

    All adapters MUST use COMPLIANCE mode (not GOVERNANCE) for regulated
    deployments.  The chain log (``_capsule_log/``) is explicitly excluded from
    WORM — only data objects and NovaSeal siblings are locked.
    """

    # -----------------------------------------------------------------------
    # Core object operations
    # -----------------------------------------------------------------------

    @abstractmethod
    def put_object(
        self,
        key: str,
        data: bytes,
        sha256_hex: str,
        retention_days: int,
        content_type: str = "application/octet-stream",
    ) -> WormPutResult:
        """Upload *data* to *key* with WORM retention.

        Implementations MUST set the backend-native checksum header so that
        in-flight corruption is detected (FR-15):
        - S3/MinIO/Ceph: ``x-amz-checksum-sha256: base64(sha256)``
        - Azure Blob: ``Content-MD5`` + ``x-ms-meta-sha256``

        Args:
            key:            Object store key.
            data:           Raw bytes to upload.
            sha256_hex:     Pre-computed SHA-256 hex of *data*.
            retention_days: WORM retention in calendar days.
            content_type:   MIME type for Content-Type header.

        Returns:
            WormPutResult with confirmation token and locked_until timestamp.

        Raises:
            CASMismatchError: if backend rejects the checksum (FR-15).
        """

    @abstractmethod
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
        """Upload two objects with identical WORM policy (FR-07).

        Used by ``put_capsule()`` step 2+4 to ensure data object and NovaSeal
        sibling receive the *same* Object Lock policy in a single adapter call.

        Returns:
            Tuple of ``(result_a, result_b)``.
        """

    @abstractmethod
    def get_object(self, key: str) -> bytes:
        """Download and return the raw bytes at *key*."""

    @abstractmethod
    def put_log_object(self, key: str, data: bytes) -> str:
        """Upload a chain-log object (no WORM policy applied — FR constraint).

        Returns the confirmation token (e.g. ETag).
        """

    @abstractmethod
    def put_log_object_if_absent(self, key: str, data: bytes) -> str:
        """Conditional-PUT: succeed only if *key* does not yet exist.

        Used for first-commit chain writes (FR-02: ``If-None-Match: *``
        semantics).

        Returns:
            Confirmation token.

        Raises:
            ConditionalPutConflict: if key already exists (412 equivalent).
        """

    @abstractmethod
    def list_objects(self, prefix: str) -> list[str]:
        """List all object keys under *prefix* (non-recursive, sorted)."""

    def iter_objects(self, prefix: str) -> Iterator[str]:
        """Stream object keys under *prefix* without materializing them all (ADR-0175).

        The default implementation delegates to :meth:`list_objects`, which is
        adequate for small namespaces and the in-memory adapter. Cloud backends
        override this to yield keys directly from their native paginators so a
        namespace with millions of keys never has to fit in memory (OQ-028).

        Unlike :meth:`list_objects`, this method does **not** guarantee global
        sort order — streaming overrides yield in backend/page order. Callers
        that need sorted output must use :meth:`list_objects`.
        """
        yield from self.list_objects(prefix)

    @abstractmethod
    def delete_object(self, key: str) -> None:
        """Attempt to delete *key*.

        WORM-locked objects will raise a backend error (FR-07, FR-09).
        This method MUST NOT suppress that error — callers must catch it.
        """

    @abstractmethod
    def object_exists(self, key: str) -> bool:
        """Return True if *key* exists in the backend."""


# ---------------------------------------------------------------------------
# Sentinel exception for conditional-PUT conflicts
# ---------------------------------------------------------------------------

class ConditionalPutConflict(Exception):
    """Raised by ``put_log_object_if_absent()`` when the key already exists."""

    def __init__(self, key: str) -> None:
        super().__init__(f"Conditional-PUT conflict: key already exists: {key!r}")
        self.key = key
