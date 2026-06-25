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
"""Exceptions for the Object Capsule Store (Phase 4 — cap-001..cap-004)."""

from __future__ import annotations


class ObjectCapsuleStoreError(Exception):
    """Base class for all Object Capsule Store errors."""


class CapsuleHashMismatch(ObjectCapsuleStoreError):
    """Raised when caller-asserted SHA-256 does not match computed SHA-256.

    Enforced client-side *before* any network call (FR-14).
    """

    def __init__(self, expected: str, computed: str) -> None:
        super().__init__(
            f"Capsule SHA-256 mismatch: expected={expected!r}, computed={computed!r}"
        )
        self.expected = expected
        self.computed = computed


class CASMismatchError(ObjectCapsuleStoreError):
    """Raised when backend-verified checksum does not match declared checksum.

    Used for in-flight corruption detection (FR-15).
    """

    def __init__(self, key: str, expected: str, observed: str) -> None:
        super().__init__(
            f"CAS mismatch for key={key!r}: expected={expected!r}, observed={observed!r}"
        )
        self.key = key
        self.expected = expected
        self.observed = observed


class ChainCommitConflict(ObjectCapsuleStoreError):
    """Raised after all retries are exhausted on conditional-PUT concurrency (FR-02).

    The caller should treat this as a transient error and retry the entire
    ``put_capsule()`` operation.
    """

    def __init__(self, tenant: str, run_id: str, retries: int) -> None:
        super().__init__(
            f"Chain commit conflict for tenant={tenant!r} run_id={run_id!r} "
            f"after {retries} retries"
        )
        self.tenant = tenant
        self.run_id = run_id
        self.retries = retries


class NovaSealUnavailable(ObjectCapsuleStoreError):
    """Raised when the NovaSeal signing service is unavailable.

    Triggers Local WAL fallback and returns ``CapsuleReceipt(status=PENDING_SIGNATURE)``
    (FR-06).
    """

    def __init__(self, cause: str) -> None:
        super().__init__(f"NovaSeal unavailable: {cause}")
        self.cause = cause


class CapsuleIntegrityError(ObjectCapsuleStoreError):
    """Raised when SHA-256 of fetched bytes does not match the chain-stored pin.

    Indicates tampering or corruption in the backend.  The chain pin is the
    authoritative source of truth; a mismatch means the object was modified
    after it was sealed.
    """

    def __init__(self, capsule_uri: str, expected_sha256: str, observed_sha256: str) -> None:
        super().__init__(
            f"SHA-256 integrity failure for {capsule_uri!r}: "
            f"expected={expected_sha256!r}, observed={observed_sha256!r}"
        )
        self.capsule_uri = capsule_uri
        self.expected_sha256 = expected_sha256
        self.observed_sha256 = observed_sha256


class WormPolicyViolation(ObjectCapsuleStoreError):
    """Raised when a WORM operation would violate the retention policy (FR-07, FR-09)."""


class ChainIntegrityError(ObjectCapsuleStoreError):
    """Raised when manifest chain integrity verification fails on read (OQ-027).

    Covers two failure modes:
    - Version gap or duplicate: the sequence {v1, v2, ...} is not contiguous.
    - Hash-chain break: ``prev_commit_hash`` of commit N+1 does not match
      SHA-256(serialised commit N).
    """

    def __init__(self, tenant: str, run_id: str, reason: str) -> None:
        super().__init__(
            f"Chain integrity failure for tenant={tenant!r} run_id={run_id!r}: {reason}"
        )
        self.tenant = tenant
        self.run_id = run_id
        self.reason = reason


class CompressionDictNotFound(ObjectCapsuleStoreError):
    """Raised when compression_dict_id references an unknown dictionary.

    Either the dictionary ID was never trained/registered, or a ZstdDictRegistry
    was not provided to ObjectCapsuleStore but a capsule stored with a
    compression_dict_id is being read back.
    """

    def __init__(self, dict_id: str) -> None:
        super().__init__(
            f"Compression dictionary not found: dict_id={dict_id!r}. "
            "Ensure a ZstdDictRegistry with this dictionary is passed to "
            "ObjectCapsuleStore."
        )
        self.dict_id = dict_id
