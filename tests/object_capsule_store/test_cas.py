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
"""Tests for CAS Validator (FR-14, FR-15, FR-17)."""

from __future__ import annotations

import hashlib
import os
import struct

import pytest

from novafabric.object_capsule_store.cas import (
    canonical_bytes,
    compute_sha256,
    derive_key,
    derive_novaseal_key,
    sha256_to_base64,
    validate_sha256,
)
from novafabric.object_capsule_store.exceptions import CapsuleHashMismatch

# ---------------------------------------------------------------------------
# FR-17: only SHA-256 — verify we are NOT using BLAKE3/MD5/SHA-3
# ---------------------------------------------------------------------------

def test_compute_sha256_uses_sha256_only():
    """FR-17: Verify canonical byte stream uses SHA-256 only."""
    content = b"hello capsule"
    result = compute_sha256(content)
    # Must be 64 hex chars (256 bits)
    assert len(result) == 64
    assert all(c in "0123456789abcdef" for c in result)
    # Verify manually
    canon = b"capsule_v1:" + struct.pack(">Q", len(content)) + b"\x00" + content
    expected = hashlib.sha256(canon).hexdigest()
    assert result == expected


def test_canonical_bytes_structure():
    """canonical_bytes must follow: prefix + 8-byte big-endian len + \\x00 + content."""
    content = b"test"
    result = canonical_bytes(content)
    assert result[:11] == b"capsule_v1:"
    length = struct.unpack(">Q", result[11:19])[0]
    assert length == len(content)
    assert result[19:20] == b"\x00"
    assert result[20:] == content


def test_canonical_bytes_empty_content():
    """Empty content must produce valid canonical bytes."""
    result = canonical_bytes(b"")
    assert len(result) == 11 + 8 + 1  # prefix + 8-byte len + \x00


# ---------------------------------------------------------------------------
# FR-14: SHA-256 key derivation reproducibility
# ---------------------------------------------------------------------------

def test_key_derivation_reproducible():
    """1,000 random payloads → keys reproducible from content (FR-14)."""
    for i in range(1000):
        content = os.urandom(64 + i % 512)
        sha256 = compute_sha256(content)
        key1 = derive_key("tenant_a", sha256)
        key2 = derive_key("tenant_a", sha256)
        assert key1 == key2
        assert key1 == f"capsules/tenant_a/{sha256[0:4]}/{sha256}/data.zst"


def test_key_derivation_format():
    sha256 = "a" * 64
    key = derive_key("myorg", sha256)
    assert key == f"capsules/myorg/{'a'*4}/{sha256}/data.zst"


def test_novaseal_key_derivation_format():
    sha256 = "b" * 64
    key = derive_novaseal_key("tenant_x", sha256)
    assert key == f"capsules/tenant_x/{'b'*4}/{sha256}/novaseal.json"


def test_derive_key_invalid_sha256():
    with pytest.raises(ValueError, match="64 hex chars"):
        derive_key("t", "short_hash")


# ---------------------------------------------------------------------------
# FR-14: validate_sha256 raises CapsuleHashMismatch before network call
# ---------------------------------------------------------------------------

def test_validate_sha256_ok():
    content = b"valid capsule bytes"
    sha256 = compute_sha256(content)
    assert validate_sha256(content, sha256) == sha256


def test_validate_sha256_mismatch_raises_before_network():
    """Tampered fixtures must raise CapsuleHashMismatch BEFORE any network call."""
    content = b"original"
    wrong_sha256 = "0" * 64
    with pytest.raises(CapsuleHashMismatch) as exc_info:
        validate_sha256(content, wrong_sha256)
    assert exc_info.value.expected == wrong_sha256
    assert exc_info.value.computed != wrong_sha256


def test_capsule_hash_mismatch_1000_random():
    """1,000 tampered fixtures → CapsuleHashMismatch (FR-14 acceptance)."""
    for _ in range(1000):
        content = os.urandom(128)
        real_sha = compute_sha256(content)
        tampered = bytes([b ^ 0xFF for b in content[:16]]) + content[16:]
        with pytest.raises(CapsuleHashMismatch):
            validate_sha256(tampered, real_sha)


# ---------------------------------------------------------------------------
# sha256_to_base64 for S3 checksum header (FR-15)
# ---------------------------------------------------------------------------

def test_sha256_to_base64_roundtrip():
    import base64

    sha256_hex = "a" * 64
    b64 = sha256_to_base64(sha256_hex)
    decoded = base64.b64decode(b64)
    assert decoded == bytes.fromhex(sha256_hex)
