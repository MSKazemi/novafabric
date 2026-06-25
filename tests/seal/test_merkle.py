"""Tests for the SQLite-backed append-only Merkle log (NovaSeal v0.1)."""

from __future__ import annotations

import pytest

from novafabric.trust.novaseal.merkle import (
    MerkleError,
    MerkleLog,
    _compute_inclusion_proof,
    _compute_root,
    _empty_hash,
    _leaf_hash,
    _node_hash,
    verify_inclusion_proof,
)

# ---------------------------------------------------------------------------
# Pure Merkle primitives
# ---------------------------------------------------------------------------

class TestMerkleHashes:
    def test_leaf_hash_domain_separation(self):
        # Leaf hash must differ from raw SHA-256 (domain separator \x00)
        import hashlib
        data = b"hello"
        leaf = _leaf_hash(data)
        raw = hashlib.sha256(data).hexdigest()
        assert leaf != raw

    def test_node_hash_domain_separation(self):
        import hashlib
        left = "a" * 64
        right = "b" * 64
        node = _node_hash(left, right)
        raw = hashlib.sha256(bytes.fromhex(left) + bytes.fromhex(right)).hexdigest()
        assert node != raw

    def test_leaf_hash_deterministic(self):
        data = b"test"
        assert _leaf_hash(data) == _leaf_hash(data)

    def test_node_hash_not_commutative(self):
        left, right = "a" * 64, "b" * 64
        assert _node_hash(left, right) != _node_hash(right, left)

    def test_empty_hash(self):
        import hashlib
        assert _empty_hash() == hashlib.sha256(b"").hexdigest()


class TestComputeRoot:
    def test_empty_tree(self):
        assert _compute_root([]) == _empty_hash()

    def test_single_leaf(self):
        leaf = _leaf_hash(b"only")
        root = _compute_root([leaf])
        assert root == leaf

    def test_two_leaves(self):
        l1 = _leaf_hash(b"a")
        l2 = _leaf_hash(b"b")
        root = _compute_root([l1, l2])
        assert root == _node_hash(l1, l2)

    def test_three_leaves_padded(self):
        leaves = [_leaf_hash(b"a"), _leaf_hash(b"b"), _leaf_hash(b"c")]
        root = _compute_root(leaves)
        # 3 leaves → padded to 4: [l0, l1, l2, l2]
        # Level 1: [node(l0,l1), node(l2,l2)]
        # Level 2: node(node(l0,l1), node(l2,l2))
        expected = _node_hash(
            _node_hash(leaves[0], leaves[1]),
            _node_hash(leaves[2], leaves[2]),
        )
        assert root == expected

    def test_deterministic(self):
        leaves = [_leaf_hash(b"x"), _leaf_hash(b"y")]
        assert _compute_root(leaves) == _compute_root(leaves)


class TestInclusionProof:
    def test_two_leaves_proof_leaf0(self):
        l0 = _leaf_hash(b"a")
        l1 = _leaf_hash(b"b")
        proof = _compute_inclusion_proof(0, [l0, l1])
        assert proof == [l1]

    def test_two_leaves_proof_leaf1(self):
        l0 = _leaf_hash(b"a")
        l1 = _leaf_hash(b"b")
        proof = _compute_inclusion_proof(1, [l0, l1])
        assert proof == [l0]

    def test_out_of_range_raises(self):
        with pytest.raises(MerkleError, match="out of range"):
            _compute_inclusion_proof(5, [_leaf_hash(b"a")])

    def test_empty_tree_raises(self):
        with pytest.raises(MerkleError, match="Empty"):
            _compute_inclusion_proof(0, [])

    def test_verify_inclusion_proof_valid(self):
        leaves = [_leaf_hash(bytes([i])) for i in range(4)]
        root = _compute_root(leaves)
        for idx in range(4):
            proof = _compute_inclusion_proof(idx, leaves)
            assert verify_inclusion_proof(leaves[idx], idx, proof, root, 4)

    def test_verify_inclusion_proof_invalid(self):
        leaves = [_leaf_hash(bytes([i])) for i in range(4)]
        root = _compute_root(leaves)
        proof = _compute_inclusion_proof(0, leaves)
        # Tamper with proof
        tampered_proof = [("f" * 64)] + proof[1:]
        assert not verify_inclusion_proof(leaves[0], 0, tampered_proof, root, 4)


# ---------------------------------------------------------------------------
# MerkleLog (SQLite)
# ---------------------------------------------------------------------------

class TestMerkleLog:
    def test_append_single(self, tmp_path):
        log = MerkleLog(tmp_path / "test.db")
        entry = log.append({"capsule_id": "abc123"})
        assert entry["leaf_index"] == 0
        assert entry["tree_size"] == 1
        assert len(entry["leaf_hash"]) == 64  # hex
        log.close()

    def test_append_multiple_increments_index(self, tmp_path):
        log = MerkleLog(tmp_path / "test.db")
        for i in range(5):
            entry = log.append({"n": i})
            assert entry["leaf_index"] == i
            assert entry["tree_size"] == i + 1
        log.close()

    def test_root_changes_on_append(self, tmp_path):
        log = MerkleLog(tmp_path / "test.db")
        e1 = log.append({"x": 1})
        root1 = e1["root_hash"]
        e2 = log.append({"x": 2})
        root2 = e2["root_hash"]
        assert root1 != root2
        log.close()

    def test_get_leaf(self, tmp_path):
        log = MerkleLog(tmp_path / "test.db")
        log.append({"id": "first"})
        log.append({"id": "second"})
        leaf = log.get_leaf(1)
        assert leaf is not None
        assert leaf["id"] == "second"
        log.close()

    def test_get_leaf_out_of_range(self, tmp_path):
        log = MerkleLog(tmp_path / "test.db")
        log.append({"x": 1})
        assert log.get_leaf(99) is None
        log.close()

    def test_inclusion_proof(self, tmp_path):
        log = MerkleLog(tmp_path / "test.db")
        for i in range(4):
            log.append({"i": i})
        proof = log.get_inclusion_proof(0)
        assert isinstance(proof, list)
        log.close()

    def test_verify_entry(self, tmp_path):
        log = MerkleLog(tmp_path / "test.db")
        for i in range(3):
            log.append({"i": i})
        for i in range(3):
            assert log.verify_entry(i) is True
        log.close()

    def test_verify_entry_out_of_range(self, tmp_path):
        log = MerkleLog(tmp_path / "test.db")
        log.append({"x": 1})
        assert log.verify_entry(99) is False
        log.close()

    def test_append_only_trigger_delete(self, tmp_path):
        """SQLite trigger must reject DELETE on leaves."""
        import sqlite3
        log = MerkleLog(tmp_path / "test.db")
        log.append({"x": 1})
        # RAISE(FAIL,...) maps to IntegrityError in Python sqlite3
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            log._conn.execute("DELETE FROM leaves WHERE leaf_index = 0")
        log.close()

    def test_append_only_trigger_update(self, tmp_path):
        """SQLite trigger must reject UPDATE on leaves."""
        import sqlite3
        log = MerkleLog(tmp_path / "test.db")
        log.append({"x": 1})
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            log._conn.execute(
                "UPDATE leaves SET leaf_hash = 'bad' WHERE leaf_index = 0"
            )
        log.close()

    def test_current_root_empty(self, tmp_path):
        log = MerkleLog(tmp_path / "test.db")
        assert log.current_root() is None
        log.close()

    def test_current_root_after_append(self, tmp_path):
        log = MerkleLog(tmp_path / "test.db")
        log.append({"x": 1})
        root = log.current_root()
        assert root is not None
        assert len(root) == 64
        log.close()

    def test_tree_size(self, tmp_path):
        log = MerkleLog(tmp_path / "test.db")
        assert log.tree_size() == 0
        log.append({"x": 1})
        assert log.tree_size() == 1
        log.append({"x": 2})
        assert log.tree_size() == 2
        log.close()

    def test_persists_across_instances(self, tmp_path):
        """Log entries survive closing and reopening the DB."""
        db = tmp_path / "persist.db"
        log1 = MerkleLog(db)
        log1.append({"id": "first"})
        log1.close()

        log2 = MerkleLog(db)
        assert log2.tree_size() == 1
        assert log2.get_leaf(0) == {"id": "first"}
        log2.close()
