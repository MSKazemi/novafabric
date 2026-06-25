"""Tests for MerkleLog.verify_consistency() and ConsistencyResult."""

from __future__ import annotations

from novafabric.trust.novaseal.merkle import ConsistencyResult, MerkleLog


class TestLogVerifyEmpty:
    def test_empty_log_is_consistent(self, tmp_path):
        """An empty MerkleLog returns consistent=True, leaf_count=0."""
        log = MerkleLog(tmp_path / "empty.db")
        result = log.verify_consistency()
        assert result.consistent is True
        assert result.leaf_count == 0
        assert result.errors == []
        log.close()


class TestLogVerifyConsistent:
    def test_single_entry_consistent(self, tmp_path):
        """Append one entry; verify_consistency returns consistent=True, leaf_count=1."""
        log = MerkleLog(tmp_path / "single.db")
        log.append({"capsule_id": "cap-001"})
        result = log.verify_consistency()
        assert result.consistent is True
        assert result.leaf_count == 1
        assert result.errors == []
        log.close()

    def test_multiple_entries_consistent(self, tmp_path):
        """Append 5 entries; verify_consistency passes."""
        log = MerkleLog(tmp_path / "multi.db")
        for i in range(5):
            log.append({"index": i, "data": f"entry-{i}"})
        result = log.verify_consistency()
        assert result.consistent is True
        assert result.leaf_count == 5
        assert result.errors == []
        log.close()

    def test_root_hash_populated(self, tmp_path):
        """verify_consistency populates root_hash for a non-empty log."""
        log = MerkleLog(tmp_path / "rooted.db")
        log.append({"x": 1})
        result = log.verify_consistency()
        assert len(result.root_hash) == 64  # SHA-256 hex
        log.close()


class TestLogVerifyTampered:
    def test_tampered_leaf_hash_detected(self, tmp_path):
        """Manually corrupting a leaf_hash causes verify_consistency to return consistent=False."""
        log = MerkleLog(tmp_path / "tamper_leaf.db")
        log.append({"capsule_id": "cap-tamper"})
        log.append({"capsule_id": "cap-tamper-2"})

        # Bypass the append-only trigger by using a raw connection
        import sqlite3
        raw = sqlite3.connect(str(tmp_path / "tamper_leaf.db"))
        raw.execute("DROP TRIGGER IF EXISTS reject_leaf_update")
        raw.execute(
            "UPDATE leaves SET leaf_hash = ? WHERE leaf_index = 0",
            ("a" * 64,),
        )
        raw.commit()
        raw.close()

        # Re-open via MerkleLog (creates a fresh connection)
        log2 = MerkleLog(tmp_path / "tamper_leaf.db")
        result = log2.verify_consistency()
        assert result.consistent is False
        assert len(result.errors) >= 1
        assert any("leaf 0" in e for e in result.errors)
        log2.close()

    def test_tampered_root_detected(self, tmp_path):
        """Manually corrupting a tree_head root_hash causes verify_consistency to detect mismatch."""
        log = MerkleLog(tmp_path / "tamper_root.db")
        log.append({"capsule_id": "cap-root-tamper"})
        tree_size = log.tree_size()

        # Corrupt the tree_head directly
        log._conn.execute(
            "UPDATE tree_heads SET root_hash = ? WHERE tree_size = ?",
            ("b" * 64, tree_size),
        )
        log._conn.commit()

        result = log.verify_consistency()
        assert result.consistent is False
        assert any("root mismatch" in e for e in result.errors)
        log.close()


class TestConsistencyResultFields:
    def test_consistency_result_fields(self, tmp_path):
        """ConsistencyResult has leaf_count, root_hash, consistent, errors."""
        log = MerkleLog(tmp_path / "fields.db")
        log.append({"k": "v"})
        result = log.verify_consistency()

        assert hasattr(result, "leaf_count")
        assert hasattr(result, "root_hash")
        assert hasattr(result, "consistent")
        assert hasattr(result, "errors")
        assert isinstance(result.errors, list)
        log.close()

    def test_consistency_result_is_dataclass(self):
        """ConsistencyResult can be constructed directly."""
        r = ConsistencyResult(leaf_count=3, root_hash="abc", consistent=True)
        assert r.leaf_count == 3
        assert r.root_hash == "abc"
        assert r.consistent is True
        assert r.errors == []

    def test_consistency_result_errors_default(self):
        """ConsistencyResult.errors defaults to empty list."""
        r = ConsistencyResult(leaf_count=0, root_hash="", consistent=True)
        assert r.errors == []
        # Verify it's a new list per instance (not shared)
        r2 = ConsistencyResult(leaf_count=0, root_hash="", consistent=True)
        r2.errors.append("err")
        assert r.errors == []
