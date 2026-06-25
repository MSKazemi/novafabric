"""Tests for LocalWormAdapter (Track W-0)."""
from __future__ import annotations

import sqlite3

import pytest

from novafabric.storage._local_worm import LocalWormAdapter
from novafabric.storage.worm import IntegrityResult, WormAdapter


def test_local_worm_is_worm_adapter(tmp_path):
    adapter = LocalWormAdapter(tmp_path / "worm.db")
    assert isinstance(adapter, WormAdapter)


def test_put_and_get(tmp_path):
    adapter = LocalWormAdapter(tmp_path / "worm.db")
    receipt = adapter.put("cap1", b"hello", retention_days=30)
    assert adapter.get("cap1") == b"hello"
    assert receipt.capsule_id == "cap1"
    assert receipt.backend_type == "local"


def test_put_idempotent(tmp_path):
    adapter = LocalWormAdapter(tmp_path / "worm.db")
    adapter.put("cap1", b"hello", retention_days=30)
    adapter.put("cap1", b"different", retention_days=30)  # second is ignored
    assert adapter.get("cap1") == b"hello"  # original preserved


def test_get_missing_raises(tmp_path):
    adapter = LocalWormAdapter(tmp_path / "worm.db")
    with pytest.raises(KeyError):
        adapter.get("missing")


def test_lock_adds_hold(tmp_path):
    db = tmp_path / "worm.db"
    adapter = LocalWormAdapter(db)
    adapter.put("cap1", b"data", retention_days=30)
    adapter.lock("cap1", "hold-001")
    adapter.lock("cap1", "hold-002")
    # Verify both hold_ids are present by querying the DB directly
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT hold_ids FROM worm_capsules WHERE capsule_id='cap1'"
        ).fetchone()
    assert row is not None
    hold_ids_str = row[0]
    assert "hold-001" in hold_ids_str
    assert "hold-002" in hold_ids_str


def test_list_prefix(tmp_path):
    adapter = LocalWormAdapter(tmp_path / "worm.db")
    adapter.put("run/cap1", b"a", retention_days=30)
    adapter.put("run/cap2", b"b", retention_days=30)
    adapter.put("other/cap3", b"c", retention_days=30)
    results = adapter.list(prefix="run/")
    assert len(results) == 2
    ids = {e.capsule_id for e in results}
    assert ids == {"run/cap1", "run/cap2"}


def test_verify_integrity_ok(tmp_path):
    adapter = LocalWormAdapter(tmp_path / "worm.db")
    adapter.put("cap1", b"data", retention_days=30)
    result = adapter.verify_integrity("cap1")
    assert result.ok is True
    assert result.error == ""


def test_verify_integrity_not_found(tmp_path):
    adapter = LocalWormAdapter(tmp_path / "worm.db")
    result = adapter.verify_integrity("missing")
    assert isinstance(result, IntegrityResult)
    assert result.ok is False
    assert "not found" in result.error


def test_lock_missing_raises(tmp_path):
    a = LocalWormAdapter(tmp_path / "worm.db")
    with pytest.raises(KeyError):
        a.lock("nonexistent", "hold-001")


def test_lock_rejects_comma_in_hold_id(tmp_path):
    a = LocalWormAdapter(tmp_path / "worm.db")
    a.put("cap1", b"data", retention_days=30)
    with pytest.raises(ValueError, match="commas"):
        a.lock("cap1", "hold,evil")
