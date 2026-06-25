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
"""Tests for verify_capsule (FR-16)."""

from __future__ import annotations

from novafabric.object_capsule_store.backend_router import InMemoryWormAdapter
from novafabric.object_capsule_store.cas import compute_sha256, derive_key
from novafabric.object_capsule_store.rebuild import verify_capsule


def test_verify_healthy_capsule():
    """FR-16: Healthy capsule → ok=True."""
    adapter = InMemoryWormAdapter()
    content = b"capsule payload"
    sha256 = compute_sha256(content)
    key = derive_key("org", sha256)

    # Store content (InMemoryWormAdapter stores raw bytes for data objects)
    # For verify_capsule, we store the raw content (not canonical-wrapped)
    # because get_object returns raw bytes and compute_sha256 wraps them
    adapter.put_log_object(key, content)

    result = verify_capsule(adapter, key, sha256)
    assert result.ok is True
    assert result.observed_sha256 == sha256


def test_verify_tampered_capsule():
    """FR-16: Tampered capsule → ok=False with sha256 values populated."""
    adapter = InMemoryWormAdapter()
    content = b"original capsule"
    sha256 = compute_sha256(content)
    key = derive_key("org", sha256)

    adapter.put_log_object(key, content)

    # Compute what SHA-256 the "tampered" content would have
    tampered = b"tampered capsule"
    tampered_sha256 = compute_sha256(tampered)

    # Now overwrite with tampered content
    adapter._store[key] = tampered

    result = verify_capsule(adapter, key, sha256)
    assert result.ok is False
    assert result.expected_sha256 == sha256
    assert result.observed_sha256 == tampered_sha256


def test_verify_missing_capsule():
    """FR-16: Missing capsule → ok=False with error."""
    adapter = InMemoryWormAdapter()
    result = verify_capsule(adapter, "capsules/t/abcd/missing/data.zst", "a" * 64)
    assert result.ok is False
    assert result.error is not None
