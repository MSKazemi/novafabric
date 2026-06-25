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
"""Tests for get_capsule(verify_chain=True) and verify_capsule_chain() (A-6).

Verifies that the manifest chain prev_commit_hash walk is enforced when
requested and bypassed by default (keeping normal reads O(1)).
"""

from __future__ import annotations

import hashlib

import pytest

from novafabric.object_capsule_store.exceptions import (
    CapsuleIntegrityError,
    ChainIntegrityError,
)
from novafabric.object_capsule_store.manifest_chain import _version_key
from novafabric.object_capsule_store.models import ManifestCommit

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_capsule(seed: int = 0, size: int = 256) -> bytes:
    base = hashlib.sha256(seed.to_bytes(8, "big")).digest()
    return (base * (size // 32 + 1))[:size]


# ---------------------------------------------------------------------------
# Happy path: verify_capsule_chain() on a valid chain
# ---------------------------------------------------------------------------


def test_verify_capsule_chain_valid_chain(store):
    """verify_capsule_chain() succeeds on a legitimate unmodified chain."""
    content = make_capsule(seed=100)
    receipt = store.put_capsule(tenant="org", run_id="vc1", content_bytes=content)

    result = store.verify_capsule_chain(
        tenant="org", run_id="vc1", capsule_uri=receipt.capsule_uri
    )
    assert result == content


def test_verify_capsule_chain_multi_commit_valid(store):
    """verify_capsule_chain() succeeds on a chain with multiple commits."""
    content1 = make_capsule(seed=101)
    content2 = make_capsule(seed=102)
    content3 = make_capsule(seed=103)

    receipt1 = store.put_capsule(tenant="org", run_id="vc2", content_bytes=content1)
    store.put_capsule(tenant="org", run_id="vc2", content_bytes=content2)
    store.put_capsule(tenant="org", run_id="vc2", content_bytes=content3)

    result = store.verify_capsule_chain(
        tenant="org", run_id="vc2", capsule_uri=receipt1.capsule_uri
    )
    assert result == content1


# ---------------------------------------------------------------------------
# Error path: verify_capsule_chain() on a tampered chain
# ---------------------------------------------------------------------------


def test_verify_capsule_chain_detects_tampered_chain(store, mem_adapter):
    """verify_capsule_chain() raises ChainIntegrityError when a chain link is broken."""
    content1 = make_capsule(seed=110)
    content2 = make_capsule(seed=111)

    store.put_capsule(tenant="org", run_id="vc3", content_bytes=content1)
    receipt2 = store.put_capsule(tenant="org", run_id="vc3", content_bytes=content2)

    # Tamper: overwrite the v1 chain commit with a modified capsule_sha256.
    # v2's prev_commit_hash was computed against the original v1, so after
    # tampering the hash-chain check must detect the discrepancy.
    key_v1 = _version_key("org", "vc3", 1)
    original_v1 = ManifestCommit.model_validate_json(mem_adapter.get_object(key_v1))
    tampered_v1 = original_v1.model_copy(update={"capsule_sha256": "a" * 64})
    mem_adapter._store[key_v1] = tampered_v1.model_dump_json().encode("utf-8")  # type: ignore[attr-defined]

    with pytest.raises(ChainIntegrityError, match="Hash-chain break"):
        store.verify_capsule_chain(
            tenant="org", run_id="vc3", capsule_uri=receipt2.capsule_uri
        )


def test_verify_capsule_chain_detects_version_gap(store, mem_adapter):
    """verify_capsule_chain() raises ChainIntegrityError when a commit is missing."""
    content1 = make_capsule(seed=120)
    content2 = make_capsule(seed=121)
    content3 = make_capsule(seed=122)

    store.put_capsule(tenant="org", run_id="vc4", content_bytes=content1)
    store.put_capsule(tenant="org", run_id="vc4", content_bytes=content2)
    receipt3 = store.put_capsule(tenant="org", run_id="vc4", content_bytes=content3)

    # Remove the v2 commit to create a gap in the chain.
    key_v2 = _version_key("org", "vc4", 2)
    del mem_adapter._store[key_v2]  # type: ignore[attr-defined]

    with pytest.raises(ChainIntegrityError, match="Version contiguity failure"):
        store.verify_capsule_chain(
            tenant="org", run_id="vc4", capsule_uri=receipt3.capsule_uri
        )


# ---------------------------------------------------------------------------
# Default behaviour: get_capsule() without verify_chain does NOT raise on broken chain
# ---------------------------------------------------------------------------


def test_get_capsule_explicit_false_skips_chain_verification(store, mem_adapter):
    """get_capsule(verify_chain=False) does not raise on a tampered chain.

    This confirms the opt-out path is O(1) in the chain length: the version-
    contiguity / hash-chain checks are skipped when the caller explicitly passes
    verify_chain=False (e.g., performance-critical bulk rebuild scans).
    Note: the default is now verify_chain=True (OQ-027, G-A6).
    """
    content1 = make_capsule(seed=130)
    content2 = make_capsule(seed=131)

    receipt1 = store.put_capsule(tenant="org", run_id="vc5", content_bytes=content1)
    store.put_capsule(tenant="org", run_id="vc5", content_bytes=content2)

    # Tamper: overwrite the v1 chain commit body.
    key_v1 = _version_key("org", "vc5", 1)
    original_v1 = ManifestCommit.model_validate_json(mem_adapter.get_object(key_v1))
    tampered_v1 = original_v1.model_copy(update={"stats": {"tampered": True}})
    mem_adapter._store[key_v1] = tampered_v1.model_dump_json().encode("utf-8")  # type: ignore[attr-defined]

    # Explicit verify_chain=False must succeed despite the tampered link.
    result = store.get_capsule(
        tenant="org", run_id="vc5", capsule_uri=receipt1.capsule_uri,
        verify_chain=False,
    )
    assert result == content1


# ---------------------------------------------------------------------------
# get_capsule(verify_chain=True) is equivalent to verify_capsule_chain()
# ---------------------------------------------------------------------------


def test_get_capsule_verify_chain_true_detects_tampering(store, mem_adapter):
    """get_capsule(verify_chain=True) is equivalent to verify_capsule_chain()."""
    content1 = make_capsule(seed=140)
    content2 = make_capsule(seed=141)

    store.put_capsule(tenant="org", run_id="vc6", content_bytes=content1)
    receipt2 = store.put_capsule(tenant="org", run_id="vc6", content_bytes=content2)

    # Tamper v1 commit.
    key_v1 = _version_key("org", "vc6", 1)
    original_v1 = ManifestCommit.model_validate_json(mem_adapter.get_object(key_v1))
    tampered_v1 = original_v1.model_copy(update={"capsule_sha256": "b" * 64})
    mem_adapter._store[key_v1] = tampered_v1.model_dump_json().encode("utf-8")  # type: ignore[attr-defined]

    with pytest.raises(ChainIntegrityError):
        store.get_capsule(
            tenant="org", run_id="vc6", capsule_uri=receipt2.capsule_uri,
            verify_chain=True,
        )


def test_get_capsule_verify_chain_returns_correct_bytes(store):
    """get_capsule(verify_chain=True) returns the original bytes on a valid chain."""
    content = make_capsule(seed=150)
    receipt = store.put_capsule(tenant="org", run_id="vc7", content_bytes=content)

    result = store.get_capsule(
        tenant="org", run_id="vc7", capsule_uri=receipt.capsule_uri, verify_chain=True
    )
    assert result == content


# ---------------------------------------------------------------------------
# CapsuleIntegrityError still raised when object bytes are tampered
# (even without chain tampering)
# ---------------------------------------------------------------------------


def test_verify_capsule_chain_detects_object_tampering(store, mem_adapter):
    """verify_capsule_chain() raises CapsuleIntegrityError if object bytes are modified."""
    content = make_capsule(seed=160)
    receipt = store.put_capsule(tenant="org", run_id="vc8", content_bytes=content)

    # Tamper the raw capsule object bytes (not the chain).
    mem_adapter._store[receipt.capsule_uri] = b"tampered object bytes"

    with pytest.raises(CapsuleIntegrityError):
        store.verify_capsule_chain(
            tenant="org", run_id="vc8", capsule_uri=receipt.capsule_uri
        )


# ---------------------------------------------------------------------------
# G-A6: get_capsule() verifies the full chain by default (OQ-027)
# ---------------------------------------------------------------------------


def test_get_capsule_verifies_chain_by_default_success(store):
    """get_capsule() with no arguments succeeds on a valid unmodified chain."""
    content = make_capsule(seed=170)
    receipt = store.put_capsule(tenant="org", run_id="vc9", content_bytes=content)

    # No explicit verify_chain — default must walk and pass the chain.
    result = store.get_capsule(tenant="org", run_id="vc9", capsule_uri=receipt.capsule_uri)
    assert result == content


def test_get_capsule_verifies_chain_by_default_detects_tampered_chain(store, mem_adapter):
    """get_capsule() with no arguments raises ChainIntegrityError on a corrupted chain.

    This is the core G-A6 regression test: the old default (verify_chain=False)
    silently accepted tampered chains; the new default (verify_chain=True) must
    detect and reject them.
    """
    content1 = make_capsule(seed=171)
    content2 = make_capsule(seed=172)

    store.put_capsule(tenant="org", run_id="vc10", content_bytes=content1)
    receipt2 = store.put_capsule(tenant="org", run_id="vc10", content_bytes=content2)

    # Tamper v1: change capsule_sha256 so that v2's prev_commit_hash no longer
    # matches the hash of the (now-modified) v1 commit.
    key_v1 = _version_key("org", "vc10", 1)
    original_v1 = ManifestCommit.model_validate_json(mem_adapter.get_object(key_v1))
    tampered_v1 = original_v1.model_copy(update={"capsule_sha256": "c" * 64})
    mem_adapter._store[key_v1] = tampered_v1.model_dump_json().encode("utf-8")  # type: ignore[attr-defined]

    # Default get_capsule (no verify_chain argument) must raise on the broken chain.
    with pytest.raises(ChainIntegrityError, match="Hash-chain break"):
        store.get_capsule(tenant="org", run_id="vc10", capsule_uri=receipt2.capsule_uri)
