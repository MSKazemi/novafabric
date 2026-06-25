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
"""Client-Side CAS (Content-Addressable Storage) enforcement for capsules.

cap-004 — FR-14, FR-15, FR-17.

Canonical byte stream (FR-14):
    b"capsule_v1:" + struct.pack(">Q", len(content_bytes)) + b"\\0" + content_bytes

Key derivation (FR-14):
    capsules/<tenant>/<sha256[0:4]>/<sha256>/data.zst

Only SHA-256 is used; no BLAKE3, MD5, or SHA-3 (FR-17).
"""

from __future__ import annotations

import base64
import hashlib
import struct

from novafabric.object_capsule_store.exceptions import CapsuleHashMismatch

# Canonical prefix for the CAS byte stream
_CANONICAL_PREFIX = b"capsule_v1:"


def canonical_bytes(content: bytes) -> bytes:
    """Produce the canonical byte stream for hashing.

    Args:
        content: Raw capsule payload bytes.

    Returns:
        ``b"capsule_v1:" + big-endian uint64 length + b"\\0" + content``
    """
    return _CANONICAL_PREFIX + struct.pack(">Q", len(content)) + b"\x00" + content


def compute_sha256(content: bytes) -> str:
    """Compute SHA-256 of the canonical byte stream and return lowercase hex.

    This is the *only* hash function used on the write path (FR-17).

    Args:
        content: Raw capsule payload bytes.

    Returns:
        64-character lowercase hex string.
    """
    return hashlib.sha256(canonical_bytes(content)).hexdigest()


def derive_key(tenant: str, sha256: str) -> str:
    """Derive the canonical object-store key for a capsule (FR-14).

    Key format: ``capsules/<tenant>/<sha256[0:4]>/<sha256>/data.zst``

    Args:
        tenant:  Tenant identifier (must be non-empty).
        sha256:  64-character lowercase hex SHA-256.

    Returns:
        Canonical object-store key string.

    Raises:
        ValueError: if ``sha256`` is not 64 hex characters.
    """
    if len(sha256) != 64:
        raise ValueError(f"sha256 must be 64 hex chars; got length {len(sha256)}")
    return f"capsules/{tenant}/{sha256[0:4]}/{sha256}/data.zst"


def derive_novaseal_key(tenant: str, sha256: str) -> str:
    """Derive the key for the NovaSeal sibling JSON (FR-06).

    Key format: ``capsules/<tenant>/<sha256[0:4]>/<sha256>/novaseal.json``
    """
    if len(sha256) != 64:
        raise ValueError(f"sha256 must be 64 hex chars; got length {len(sha256)}")
    return f"capsules/{tenant}/{sha256[0:4]}/{sha256}/novaseal.json"


def validate_sha256(content: bytes, asserted_sha256: str) -> str:
    """Validate that the computed SHA-256 matches the caller-asserted value.

    Raises ``CapsuleHashMismatch`` BEFORE any network call if they differ (FR-14).

    Args:
        content:        Raw capsule payload bytes.
        asserted_sha256: Caller-provided SHA-256 hex string.

    Returns:
        The validated (and computed) SHA-256 hex string.

    Raises:
        CapsuleHashMismatch: if computed != asserted.
    """
    computed = compute_sha256(content)
    if computed != asserted_sha256:
        raise CapsuleHashMismatch(expected=asserted_sha256, computed=computed)
    return computed


def sha256_to_base64(sha256_hex: str) -> str:
    """Convert a 64-char hex SHA-256 to standard base64 (for S3 checksum header, FR-15)."""
    return base64.b64encode(bytes.fromhex(sha256_hex)).decode("ascii")
