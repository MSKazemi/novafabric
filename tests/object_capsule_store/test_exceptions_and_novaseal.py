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
"""Coverage tests for exceptions and NovaSeal client."""

from __future__ import annotations

import pytest

from novafabric.object_capsule_store.exceptions import (
    CapsuleHashMismatch,
    CASMismatchError,
    ChainCommitConflict,
    NovaSealUnavailable,
    WormPolicyViolation,
)
from novafabric.object_capsule_store.novaseal_client import NovaSealClient, NovaSealDisabledClient

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

def test_capsule_hash_mismatch_fields():
    exc = CapsuleHashMismatch(expected="a" * 64, computed="b" * 64)
    assert exc.expected == "a" * 64
    assert exc.computed == "b" * 64
    assert "mismatch" in str(exc).lower()


def test_cas_mismatch_error_fields():
    exc = CASMismatchError(key="k", expected="aa", observed="bb")
    assert exc.key == "k"
    assert exc.expected == "aa"
    assert exc.observed == "bb"


def test_chain_commit_conflict_fields():
    exc = ChainCommitConflict(tenant="t", run_id="r", retries=5)
    assert exc.tenant == "t"
    assert exc.run_id == "r"
    assert exc.retries == 5


def test_novaseal_unavailable_fields():
    exc = NovaSealUnavailable(cause="timeout")
    assert exc.cause == "timeout"


def test_worm_policy_violation():
    exc = WormPolicyViolation("cannot delete")
    assert "cannot delete" in str(exc)


# ---------------------------------------------------------------------------
# NovaSealDisabledClient
# ---------------------------------------------------------------------------

def test_disabled_client_sign_raises():
    client = NovaSealDisabledClient()
    with pytest.raises(NovaSealUnavailable):
        client.sign("a" * 64)


def test_disabled_client_sign_bytes_raises():
    client = NovaSealDisabledClient()
    with pytest.raises(NovaSealUnavailable):
        client.sign_bytes(b"test")


# ---------------------------------------------------------------------------
# NovaSealClient with mock (sign_bytes path)
# ---------------------------------------------------------------------------

def test_novaseal_client_sign_bytes(mock_novaseal_client):
    """sign_bytes() returns JSON bytes containing leaf_hash."""
    import json

    result = mock_novaseal_client.sign_bytes(b"capsule bytes")
    payload = json.loads(result)
    assert "leaf_hash" in payload
    assert "signed_at" in payload
    assert "dsse_envelope" in payload


def test_novaseal_client_sign_raises_on_failure():
    """NovaSealClient.sign() wraps exceptions as NovaSealUnavailable."""
    from unittest.mock import MagicMock

    mock_seal = MagicMock()
    mock_seal.seal.side_effect = RuntimeError("signing service down")
    client = NovaSealClient(novaseal=mock_seal)
    with pytest.raises(NovaSealUnavailable):
        client.sign("a" * 64)
