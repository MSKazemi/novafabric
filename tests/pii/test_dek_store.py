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
"""Tests for DEKStore (ADR-0069, cap-001).

Tests cover: idempotent creation, erasure receipt, deferred erasure,
post-erase get_dek, distinct subjects, env-var factory, concurrent safety,
and JSON round-trip.
"""

from __future__ import annotations

import concurrent.futures
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from novafabric.pii.dek import (
    DataSubjectDEK,
    DEKStore,
    ErasureDeferredReceipt,
    ErasureReceipt,
    open_dek_store,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def store(tmp_path: Path) -> DEKStore:
    """Fresh DEKStore backed by a temp directory."""
    return DEKStore(tmp_path / "dek.db")


# ---------------------------------------------------------------------------
# Test 1: get_or_create_dek is idempotent
# ---------------------------------------------------------------------------


def test_get_or_create_dek_idempotent(store: DEKStore) -> None:
    """The same DEK is returned on repeated calls for the same subject."""
    dek_a = store.get_or_create_dek("alice@example.com")
    dek_b = store.get_or_create_dek("alice@example.com")

    assert isinstance(dek_a, DataSubjectDEK)
    assert dek_a.dek_hex == dek_b.dek_hex
    assert dek_a.subject_id == "alice@example.com"
    assert len(dek_a.dek_hex) == 64  # 32 bytes as hex


# ---------------------------------------------------------------------------
# Test 2: erase_subject with no age constraint returns ErasureReceipt
# ---------------------------------------------------------------------------


def test_erase_subject_returns_receipt(store: DEKStore) -> None:
    """Erasing a subject (no retention constraint) returns ErasureReceipt."""
    store.get_or_create_dek("bob@example.com")
    result = store.erase_subject(
        subject_id="bob@example.com",
        capsule_ids=["cap-001", "cap-002"],
        retention_months=0,  # no retention window
    )

    assert isinstance(result, ErasureReceipt)
    assert result.subject_id == "bob@example.com"
    assert result.method == "aes-256-gcm-dek-destruction"
    assert result.legal_basis == "GDPR Art.17"
    assert "cap-001" in result.capsule_ids_affected
    assert "cap-002" in result.capsule_ids_affected


# ---------------------------------------------------------------------------
# Test 3: erase_subject within retention window returns ErasureDeferredReceipt
# ---------------------------------------------------------------------------


def test_erase_subject_deferred_within_retention_window(store: DEKStore) -> None:
    """Erasing a recently-created DEK within the retention window defers erasure."""
    store.get_or_create_dek("carol@example.com")
    result = store.erase_subject(
        subject_id="carol@example.com",
        capsule_ids=[],
        retention_months=6,  # 6-month window — brand-new DEK is always within it
    )

    assert isinstance(result, ErasureDeferredReceipt)
    assert result.subject_id == "carol@example.com"
    assert result.retention_months == 6
    assert result.earliest_erasure_at > datetime.now(timezone.utc)
    assert "retention" in result.reason.lower() or "art.17" in result.reason.lower()


# ---------------------------------------------------------------------------
# Test 4: After erase, get_dek returns None
# ---------------------------------------------------------------------------


def test_get_dek_returns_none_after_erase(store: DEKStore) -> None:
    """After successful erasure, get_dek returns None for the erased subject."""
    store.get_or_create_dek("dave@example.com")

    # Verify DEK exists first.
    assert store.get_dek("dave@example.com") is not None

    store.erase_subject(
        subject_id="dave@example.com",
        capsule_ids=[],
        retention_months=0,
    )

    assert store.get_dek("dave@example.com") is None


# ---------------------------------------------------------------------------
# Test 5: DEK is different for different subject_ids
# ---------------------------------------------------------------------------


def test_dek_distinct_per_subject(store: DEKStore) -> None:
    """Different subjects receive different DEKs."""
    dek_eve = store.get_or_create_dek("eve@example.com")
    dek_frank = store.get_or_create_dek("frank@example.com")

    assert dek_eve.dek_hex != dek_frank.dek_hex
    assert dek_eve.subject_id != dek_frank.subject_id


# ---------------------------------------------------------------------------
# Test 6: open_dek_store() uses NOVAFABRIC_HOME env var
# ---------------------------------------------------------------------------


def test_open_dek_store_uses_novafabric_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """open_dek_store() creates dek.db under NOVAFABRIC_HOME."""
    monkeypatch.setenv("NOVAFABRIC_HOME", str(tmp_path))
    s = open_dek_store()
    try:
        s.get_or_create_dek("test-subject")
        assert (tmp_path / "dek.db").exists()
    finally:
        s.close()


# ---------------------------------------------------------------------------
# Test 7: Concurrent calls are safe (SQLite serialisation)
# ---------------------------------------------------------------------------


def test_concurrent_get_or_create_dek(tmp_path: Path) -> None:
    """Concurrent get_or_create_dek calls for the same subject return consistent DEKs."""
    store = DEKStore(tmp_path / "concurrent.db")
    subject = "concurrent@example.com"
    results: list[str] = []

    def worker() -> str:
        dek = store.get_or_create_dek(subject)
        return dek.dek_hex

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(worker) for _ in range(20)]
        results = [f.result() for f in concurrent.futures.as_completed(futures)]

    store.close()

    # All threads must see the same DEK hex.
    assert len(set(results)) == 1, f"Expected 1 unique DEK, got {len(set(results))}"


# ---------------------------------------------------------------------------
# Test 8: ErasureReceipt JSON round-trips correctly
# ---------------------------------------------------------------------------


def test_erasure_receipt_json_round_trip(store: DEKStore) -> None:
    """ErasureReceipt serialises to valid JSON and round-trips through model_validate."""
    store.get_or_create_dek("grace@example.com")
    receipt = store.erase_subject(
        subject_id="grace@example.com",
        capsule_ids=["capsule-abc", "capsule-xyz"],
        retention_months=0,
    )
    assert isinstance(receipt, ErasureReceipt)

    # Serialise and deserialise.
    json_str = receipt.model_dump_json()
    data = json.loads(json_str)
    restored = ErasureReceipt.model_validate(data)

    assert restored.subject_id == receipt.subject_id
    assert restored.method == receipt.method
    assert restored.legal_basis == receipt.legal_basis
    assert set(restored.capsule_ids_affected) == {"capsule-abc", "capsule-xyz"}
    assert restored.erased_at == receipt.erased_at
