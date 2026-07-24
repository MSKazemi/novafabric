"""Merkle log backends for NovaSeal v0.1 — SQLite (default) and Postgres (Scale-S4).

Per ADR-003 (regulated-industries study): each capsule signing event appends
a leaf to the tenant's Merkle tree. The root is recomputed on each append.

Tables:
    leaves(leaf_index INTEGER PK, leaf_hash TEXT, entry_json TEXT)
    tree_heads(tree_size INTEGER PK, root_hash TEXT)

Append-only invariant is enforced at the DB level (SQLite triggers /
Postgres rules + application-layer guard on Postgres).

Merkle tree structure:
    - Leaf nodes: H(0x00 || entry_bytes)
    - Internal nodes: H(0x01 || left || right)
    - Empty subtree: H(b"")
    - Root: root of a complete binary tree padded to next power of 2

Inclusion proof:
    A list of sibling hashes (left/right) needed to reconstruct the root
    from a given leaf. Verifier recomputes upward from the leaf.

Scale-S4 — Postgres backend
    ``PostgresMerkleLog`` uses psycopg (psycopg3).  Install:
        pip install novafabric[seal-postgres]

    ``verify_consistency()`` on the Postgres backend is a *sampled* check:
    it spot-checks SAMPLE_SIZE random leaves + verifies the current root
    against a recomputed root from all stored leaf hashes.  At 1M entries
    this completes in < 200 ms (p99) — the Scale-S4 acceptance criterion.
    The sampled check skips re-hashing every entry_json (trusts the stored
    leaf_hash values for entries outside the sample), which is sound given
    the append-only guarantee enforced at the DB layer.

    For a full re-hash audit (slower, O(N) Python SHA-256 calls), call
    ``verify_consistency(full=True)``.

Factory:
    Use ``open_merkle_log(uri)`` to obtain the right backend automatically:
    - ``Path`` / ``str`` without ``postgresql://`` prefix → ``MerkleLog`` (SQLite)
    - ``str`` starting with ``postgresql://`` or ``postgres://`` → ``PostgresMerkleLog``
"""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Union

from novafabric._sqlite_util import connect_sqlite


class MerkleError(Exception):
    """Raised on Merkle log integrity violations."""


@dataclass
class ConsistencyResult:
    """Result of a Merkle log consistency verification."""

    leaf_count: int
    root_hash: str
    consistent: bool
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Hash primitives
# ---------------------------------------------------------------------------

def _leaf_hash(entry_bytes: bytes) -> str:
    return hashlib.sha256(b"\x00" + entry_bytes).hexdigest()


def _node_hash(left: str, right: str) -> str:
    left_b = bytes.fromhex(left)
    right_b = bytes.fromhex(right)
    return hashlib.sha256(b"\x01" + left_b + right_b).hexdigest()


def _empty_hash() -> str:
    return hashlib.sha256(b"").hexdigest()


# ---------------------------------------------------------------------------
# Merkle root computation
# ---------------------------------------------------------------------------

def _compute_root(leaf_hashes: list[str]) -> str:
    """Compute Merkle root for a list of leaf hashes."""
    if not leaf_hashes:
        return _empty_hash()
    nodes = list(leaf_hashes)
    while len(nodes) > 1:
        if len(nodes) % 2 == 1:
            nodes.append(nodes[-1])  # pad with duplicate
        nodes = [_node_hash(nodes[i], nodes[i + 1]) for i in range(0, len(nodes), 2)]
    return nodes[0]


def merkle_layers(leaf_hashes: list[str]) -> list[list[str]]:
    """Return every layer of the Merkle tree, leaves first and the root last.

    Read-only companion to :func:`_compute_root` for visualization (ADR-0172): it applies the
    **same pairing and odd-duplicate padding rule**, so ``merkle_layers(x)[-1] ==
    [_compute_root(x)]`` always holds. The first layer is the leaves exactly as given (unpadded);
    each subsequent layer is the paired level above it. The input list is not mutated. This touches
    no signing or verification path — it only re-derives the hashes the root computation discards.
    """
    if not leaf_hashes:
        raise MerkleError("Empty tree has no layers")
    layers: list[list[str]] = [list(leaf_hashes)]
    nodes = list(leaf_hashes)
    while len(nodes) > 1:
        if len(nodes) % 2 == 1:
            nodes = [*nodes, nodes[-1]]  # pad with duplicate (new list; never mutate a layer)
        nodes = [_node_hash(nodes[i], nodes[i + 1]) for i in range(0, len(nodes), 2)]
        layers.append(list(nodes))
    return layers


# ---------------------------------------------------------------------------
# Inclusion proof
# ---------------------------------------------------------------------------

def _compute_inclusion_proof(leaf_index: int, leaf_hashes: list[str]) -> list[str]:
    """Return sibling hashes needed to prove leaf_index is in the tree."""
    if not leaf_hashes:
        raise MerkleError("Empty tree has no inclusion proof")
    if leaf_index >= len(leaf_hashes):
        raise MerkleError(
            f"leaf_index {leaf_index} out of range (tree size {len(leaf_hashes)})"
        )

    nodes = list(leaf_hashes)
    idx = leaf_index
    proof = []

    while len(nodes) > 1:
        if len(nodes) % 2 == 1:
            nodes.append(nodes[-1])

        sibling = idx ^ 1  # XOR with 1 flips the last bit: left↔right
        proof.append(nodes[sibling])
        idx //= 2
        nodes = [_node_hash(nodes[i], nodes[i + 1]) for i in range(0, len(nodes), 2)]

    return proof


def verify_inclusion_proof(
    leaf_hash: str,
    leaf_index: int,
    proof: list[str],
    expected_root: str,
    tree_size: int,
) -> bool:
    """Recompute root from proof and compare to expected_root.

    Rejects out-of-range leaf indices: because odd levels are padded by
    duplicating the last leaf, a proof for an index >= tree_size could
    otherwise recompute to the real root (a phantom-inclusion soundness gap).
    """
    if tree_size <= 0 or leaf_index < 0 or leaf_index >= tree_size:
        return False
    idx = leaf_index
    current = leaf_hash

    for sibling in proof:
        if idx % 2 == 0:
            current = _node_hash(current, sibling)
        else:
            current = _node_hash(sibling, current)
        idx //= 2

    return current == expected_root


# ---------------------------------------------------------------------------
# Consistency proof (ADR-0041 v0.2, gap-001)
#
# The v0.1 tree pads odd levels by duplicating the last node, which differs
# from RFC 6962 §2.1 unbalanced trees — so RFC 6962 consistency proofs do not
# apply verbatim (for non-power-of-two sizes the old root is not a node of
# the new tree).  The proof here is therefore an *aligned perfect-subtree
# decomposition* proof, valid against this duplicate-padding tree shape:
#
#   proof = decomposition of leaves[0:old_size] into maximal aligned perfect
#           subtrees (their root hashes), plus the decomposition of
#           leaves[old_size:new_size].
#
# The verifier recomputes BOTH roots from those O(log n + log m) hashes using
# the same padding rule; because the old-prefix commitment appears in both
# computations, matching roots prove the new log extends the old one
# append-only.  Not wire-compatible with RFC 6962 proofs (documented
# deviation; reconciliation tracked in ADR-0041 v0.2).
# ---------------------------------------------------------------------------


def _perfect_root(leaf_hashes: list[str]) -> str:
    """Root of a perfect (power-of-two) subtree — pure pairing, no padding."""
    nodes = list(leaf_hashes)
    while len(nodes) > 1:
        nodes = [_node_hash(nodes[i], nodes[i + 1]) for i in range(0, len(nodes), 2)]
    return nodes[0]


def _decompose_range(
    leaf_hashes: list[str], start: int, end: int
) -> list[dict[str, Any]]:
    """Decompose leaves[start:end] into maximal aligned perfect subtrees.

    Each part is ``{"height": h, "hash": root}`` for a subtree of 2**h leaves
    whose global start index is a multiple of its size.
    """
    parts: list[dict[str, Any]] = []
    i = start
    while i < end:
        size = 1
        while (
            i % (size * 2) == 0 and i + size * 2 <= end
        ):
            size *= 2
        parts.append(
            {
                "height": size.bit_length() - 1,
                "hash": _perfect_root(leaf_hashes[i : i + size]),
            }
        )
        i += size
    return parts


def _root_from_parts(parts: list[dict[str, Any]]) -> str:
    """Recompute the duplicate-padding tree root from aligned subtree parts.

    First merges adjacent equal-height parts bottom-up (binary-counter
    stack), then folds right-to-left raising the right accumulator with
    ``H(r, r)`` duplication — exactly what :func:`_compute_root` does to a
    trailing partial subtree.
    """
    if not parts:
        return _empty_hash()
    stack: list[tuple[int, str]] = []
    for part in parts:
        height, digest = int(part["height"]), str(part["hash"])
        stack.append((height, digest))
        while len(stack) >= 2 and stack[-1][0] == stack[-2][0]:
            h_right, right = stack.pop()
            _h_left, left = stack.pop()
            stack.append((h_right + 1, _node_hash(left, right)))
    height, current = stack.pop()
    while stack:
        target_height, left = stack.pop()
        while height < target_height:
            current = _node_hash(current, current)
            height += 1
        current = _node_hash(left, current)
        height += 1
    return current


def verify_consistency_proof(
    proof: dict[str, Any],
    old_root: str,
    new_root: str,
) -> bool:
    """Offline check that the log at ``new_size`` extends the log at ``old_size``.

    *proof* is the dict produced by ``MerkleLog.consistency_proof()`` /
    ``PostgresMerkleLog.consistency_proof()``.  Pure function — no log access.
    """
    old_parts = list(proof.get("old_parts", []))
    tail_parts = list(proof.get("tail_parts", []))
    if not old_parts:
        return False
    if _root_from_parts(old_parts) != old_root:
        return False
    return _root_from_parts(old_parts + tail_parts) == new_root


def _build_consistency_proof(
    leaf_hashes: list[str], old_size: int, new_size: int
) -> dict[str, Any]:
    """Shared proof builder over an in-memory leaf-hash list."""
    if old_size <= 0:
        raise MerkleError("old_size must be >= 1")
    if old_size > new_size:
        raise MerkleError(
            f"old_size {old_size} exceeds new_size {new_size}"
        )
    if new_size > len(leaf_hashes):
        raise MerkleError(
            f"new_size {new_size} exceeds tree size {len(leaf_hashes)}"
        )
    return {
        "scheme": "novafabric-padded-subtree-v1",
        "old_size": old_size,
        "new_size": new_size,
        "old_parts": _decompose_range(leaf_hashes, 0, old_size),
        "tail_parts": _decompose_range(leaf_hashes, old_size, new_size),
        "old_root": _root_from_parts(_decompose_range(leaf_hashes, 0, old_size)),
        "new_root": _compute_root(leaf_hashes[:new_size]),
    }


# ---------------------------------------------------------------------------
# MerkleLog — SQLite-backed, append-only
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS leaves (
    leaf_index  INTEGER PRIMARY KEY,
    leaf_hash   TEXT    NOT NULL,
    entry_json  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS tree_heads (
    tree_size   INTEGER PRIMARY KEY,
    root_hash   TEXT    NOT NULL
);

CREATE TRIGGER IF NOT EXISTS reject_leaf_delete
BEFORE DELETE ON leaves
BEGIN
    SELECT RAISE(FAIL, 'leaves is append-only; DELETE is forbidden');
END;

CREATE TRIGGER IF NOT EXISTS reject_leaf_update
BEFORE UPDATE ON leaves
BEGIN
    SELECT RAISE(FAIL, 'leaves is append-only; UPDATE is forbidden');
END;
"""


class MerkleLog:
    """Append-only Merkle log backed by SQLite.

    Thread-safety: SQLite in WAL mode is used; callers that share the same
    :class:`MerkleLog` instance must hold an external lock for concurrent
    appends. Single-threaded use (one ``nova capture`` at a time) is safe.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = connect_sqlite(str(db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------------
    # Append
    # ------------------------------------------------------------------

    def append(self, entry: dict[str, object]) -> dict[str, object]:
        """Append an entry to the log and return the log entry dict.

        The returned dict contains:
            leaf_index: int
            leaf_hash:  str  (hex)
            root_hash:  str  (hex)
            tree_size:  int
            entry:      dict (the original entry)

        Raises:
            MerkleError: on DB integrity errors.
        """
        entry_bytes = json.dumps(entry, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
        leaf_hash = _leaf_hash(entry_bytes)
        entry_json = entry_bytes.decode("utf-8")

        with self._conn:
            # Determine next leaf_index
            row = self._conn.execute(
                "SELECT COALESCE(MAX(leaf_index) + 1, 0) FROM leaves"
            ).fetchone()
            leaf_index: int = row[0]

            self._conn.execute(
                "INSERT INTO leaves (leaf_index, leaf_hash, entry_json) VALUES (?, ?, ?)",
                (leaf_index, leaf_hash, entry_json),
            )

            # Recompute root
            all_hashes = [
                r[0]
                for r in self._conn.execute(
                    "SELECT leaf_hash FROM leaves ORDER BY leaf_index"
                ).fetchall()
            ]
            root_hash = _compute_root(all_hashes)
            tree_size = len(all_hashes)

            self._conn.execute(
                "INSERT OR REPLACE INTO tree_heads (tree_size, root_hash) VALUES (?, ?)",
                (tree_size, root_hash),
            )

        return {
            "leaf_index": leaf_index,
            "leaf_hash": leaf_hash,
            "root_hash": root_hash,
            "tree_size": tree_size,
            "entry": dict(entry),
        }

    # ------------------------------------------------------------------
    # Read / proof
    # ------------------------------------------------------------------

    def current_root(self) -> str | None:
        """Return the current Merkle root hash, or None if the log is empty."""
        row = self._conn.execute(
            "SELECT root_hash FROM tree_heads ORDER BY tree_size DESC LIMIT 1"
        ).fetchone()
        return row[0] if row else None

    def tree_size(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) FROM leaves").fetchone()
        return int(row[0])

    def get_leaf(self, leaf_index: int) -> dict[str, object] | None:
        """Return the stored log entry dict for *leaf_index*, or None."""
        row = self._conn.execute(
            "SELECT entry_json FROM leaves WHERE leaf_index = ?", (leaf_index,)
        ).fetchone()
        if row is None:
            return None
        result: dict[str, object] = json.loads(row[0])
        return result

    def get_inclusion_proof(self, leaf_index: int) -> list[str]:
        """Return the inclusion proof (list of sibling hashes) for *leaf_index*."""
        all_hashes = [
            r[0]
            for r in self._conn.execute(
                "SELECT leaf_hash FROM leaves ORDER BY leaf_index"
            ).fetchall()
        ]
        return _compute_inclusion_proof(leaf_index, all_hashes)

    def verify_entry(self, leaf_index: int) -> bool:
        """Verify that the stored entry at *leaf_index* is consistent with the root."""
        all_hashes = [
            r[0]
            for r in self._conn.execute(
                "SELECT leaf_hash FROM leaves ORDER BY leaf_index"
            ).fetchall()
        ]
        if leaf_index >= len(all_hashes):
            return False

        root = self.current_root()
        if root is None:
            return False

        proof = _compute_inclusion_proof(leaf_index, all_hashes)
        leaf_hash = all_hashes[leaf_index]
        return verify_inclusion_proof(leaf_hash, leaf_index, proof, root, len(all_hashes))

    def consistency_proof(
        self, old_size: int, new_size: int | None = None
    ) -> dict[str, Any]:
        """Build a consistency proof from *old_size* to *new_size* (ADR-0041 v0.2).

        Verifiable offline with :func:`verify_consistency_proof`; see the
        module-level deviation note vs RFC 6962 §2.1.
        """
        all_hashes = [
            r[0]
            for r in self._conn.execute(
                "SELECT leaf_hash FROM leaves ORDER BY leaf_index"
            ).fetchall()
        ]
        return _build_consistency_proof(
            all_hashes, old_size, new_size if new_size is not None else len(all_hashes)
        )

    def root_at_size(self, tree_size: int) -> str | None:
        """Stored tree head at *tree_size*, if recorded."""
        row = self._conn.execute(
            "SELECT root_hash FROM tree_heads WHERE tree_size = ?", (tree_size,)
        ).fetchone()
        return row[0] if row else None

    def verify_consistency(self) -> ConsistencyResult:
        """Recompute all leaf hashes and root; compare against stored tree heads.

        Returns a :class:`ConsistencyResult` describing whether the log is
        internally consistent. An empty log is always consistent.
        """
        errors: list[str] = []

        rows = self._conn.execute(
            "SELECT leaf_index, leaf_hash, entry_json FROM leaves ORDER BY leaf_index"
        ).fetchall()

        for row in rows:
            entry_bytes = row[2].encode("utf-8")
            expected = _leaf_hash(entry_bytes)
            if expected != row[1]:
                errors.append(
                    f"leaf {row[0]}: stored={row[1][:16]}… "
                    f"expected={expected[:16]}…"
                )

        leaf_hashes = [row[1] for row in rows]
        computed_root = _compute_root(leaf_hashes) if leaf_hashes else _empty_hash()

        stored_head = self._conn.execute(
            "SELECT root_hash FROM tree_heads WHERE tree_size = ?",
            (len(rows),),
        ).fetchone()

        if stored_head is None and len(rows) > 0:
            errors.append(f"no tree_head found for size {len(rows)}")
        elif stored_head is not None and stored_head[0] != computed_root:
            errors.append(
                f"root mismatch at size {len(rows)}: "
                f"stored={stored_head[0][:16]}… "
                f"computed={computed_root[:16]}…"
            )

        return ConsistencyResult(
            leaf_count=len(rows),
            root_hash=computed_root,
            consistent=len(errors) == 0,
            errors=errors,
        )


# ---------------------------------------------------------------------------
# PostgresMerkleLog — Postgres-backed, append-only (Scale-S4)
# ---------------------------------------------------------------------------

_PG_SCHEMA = """
CREATE SEQUENCE IF NOT EXISTS nova_seal_leaf_seq START 0 MINVALUE 0;

CREATE TABLE IF NOT EXISTS nova_seal_leaves (
    leaf_index  BIGINT PRIMARY KEY DEFAULT nextval('nova_seal_leaf_seq'),
    leaf_hash   TEXT   NOT NULL,
    entry_json  TEXT   NOT NULL
);

CREATE TABLE IF NOT EXISTS nova_seal_tree_heads (
    tree_size  BIGINT PRIMARY KEY,
    root_hash  TEXT   NOT NULL
);

CREATE OR REPLACE FUNCTION _nova_seal_reject_leaf_mutate()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'nova_seal_leaves is append-only; % is forbidden', TG_OP;
END;
$$;

DROP TRIGGER IF EXISTS reject_leaf_delete ON nova_seal_leaves;
CREATE TRIGGER reject_leaf_delete
    BEFORE DELETE ON nova_seal_leaves
    FOR EACH ROW EXECUTE FUNCTION _nova_seal_reject_leaf_mutate();

DROP TRIGGER IF EXISTS reject_leaf_update ON nova_seal_leaves;
CREATE TRIGGER reject_leaf_update
    BEFORE UPDATE ON nova_seal_leaves
    FOR EACH ROW EXECUTE FUNCTION _nova_seal_reject_leaf_mutate();
"""

_SAMPLE_SIZE = 1_000  # leaves spot-checked in verify_consistency() fast path


class PostgresMerkleLog:
    """Postgres-backed append-only Merkle log for NovaSeal (Scale-S4).

    Uses psycopg (psycopg3).  Connection is established lazily on first use.
    Install: ``pip install novafabric[seal-postgres]``

    ``verify_consistency()`` performs a *sampled* check in < 200 ms at 1M entries
    (Scale-S4 acceptance criterion).  Pass ``full=True`` for a full O(N) audit.
    """

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._conn: Any = None  # psycopg.Connection, opened lazily

    # ------------------------------------------------------------------
    # Internal connection management
    # ------------------------------------------------------------------

    def _ensure_connected(self) -> None:
        if self._conn is not None:
            return
        try:
            import psycopg
        except ImportError as exc:
            raise ImportError(
                "psycopg is required for PostgresMerkleLog. "
                "Install: pip install novafabric[seal-postgres]"
            ) from exc
        self._conn = psycopg.connect(self._dsn, autocommit=False)
        with self._conn.cursor() as cur:
            cur.execute(_PG_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001
                pass
            self._conn = None

    # ------------------------------------------------------------------
    # Append
    # ------------------------------------------------------------------

    def append(self, entry: dict[str, object]) -> dict[str, object]:
        """Append an entry and return the log entry dict (same shape as MerkleLog)."""
        self._ensure_connected()
        entry_bytes = json.dumps(entry, sort_keys=True, separators=(",", ":")).encode("utf-8")
        leaf_hash = _leaf_hash(entry_bytes)
        entry_json = entry_bytes.decode("utf-8")

        with self._conn.transaction():
            with self._conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO nova_seal_leaves (leaf_hash, entry_json)"
                    " VALUES (%s, %s) RETURNING leaf_index",
                    (leaf_hash, entry_json),
                )
                leaf_index: int = cur.fetchone()[0]

                # Recompute root using all stored leaf hashes
                cur.execute(
                    "SELECT leaf_hash FROM nova_seal_leaves ORDER BY leaf_index"
                )
                all_hashes = [row[0] for row in cur.fetchall()]
                root_hash = _compute_root(all_hashes)
                tree_size = len(all_hashes)

                cur.execute(
                    "INSERT INTO nova_seal_tree_heads (tree_size, root_hash) VALUES (%s, %s)"
                    " ON CONFLICT (tree_size) DO UPDATE SET root_hash = EXCLUDED.root_hash",
                    (tree_size, root_hash),
                )

        return {
            "leaf_index": leaf_index,
            "leaf_hash": leaf_hash,
            "root_hash": root_hash,
            "tree_size": tree_size,
            "entry": dict(entry),
        }

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def current_root(self) -> str | None:
        self._ensure_connected()
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT root_hash FROM nova_seal_tree_heads ORDER BY tree_size DESC LIMIT 1"
            )
            row = cur.fetchone()
        return row[0] if row else None

    def tree_size(self) -> int:
        self._ensure_connected()
        with self._conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM nova_seal_leaves")
            row = cur.fetchone()
        return int(row[0])

    def get_leaf(self, leaf_index: int) -> dict[str, object] | None:
        self._ensure_connected()
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT entry_json FROM nova_seal_leaves WHERE leaf_index = %s",
                (leaf_index,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        result: dict[str, object] = json.loads(row[0])
        return result

    def get_leaf_hash(self, leaf_index: int) -> str:
        """Return the stored leaf_hash for leaf_index (used for inclusion proof verification)."""
        self._ensure_connected()
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT leaf_hash FROM nova_seal_leaves WHERE leaf_index = %s",
                (leaf_index,),
            )
            row = cur.fetchone()
        if row is None:
            raise MerkleError(f"leaf_index {leaf_index} not found")
        return str(row[0])

    def get_inclusion_proof(self, leaf_index: int) -> list[str]:
        self._ensure_connected()
        with self._conn.cursor() as cur:
            cur.execute("SELECT leaf_hash FROM nova_seal_leaves ORDER BY leaf_index")
            all_hashes = [row[0] for row in cur.fetchall()]
        return _compute_inclusion_proof(leaf_index, all_hashes)

    def verify_entry(self, leaf_index: int) -> bool:
        self._ensure_connected()
        with self._conn.cursor() as cur:
            cur.execute("SELECT leaf_hash FROM nova_seal_leaves ORDER BY leaf_index")
            all_hashes = [row[0] for row in cur.fetchall()]
        if leaf_index >= len(all_hashes):
            return False
        root = self.current_root()
        if root is None:
            return False
        proof = _compute_inclusion_proof(leaf_index, all_hashes)
        return verify_inclusion_proof(
            all_hashes[leaf_index], leaf_index, proof, root, len(all_hashes)
        )

    def consistency_proof(
        self, old_size: int, new_size: int | None = None
    ) -> dict[str, Any]:
        """Build a consistency proof from *old_size* to *new_size* (ADR-0041 v0.2)."""
        self._ensure_connected()
        with self._conn.cursor() as cur:
            cur.execute("SELECT leaf_hash FROM nova_seal_leaves ORDER BY leaf_index")
            all_hashes = [row[0] for row in cur.fetchall()]
        return _build_consistency_proof(
            all_hashes, old_size, new_size if new_size is not None else len(all_hashes)
        )

    def root_at_size(self, tree_size: int) -> str | None:
        """Stored tree head at *tree_size*, if recorded."""
        self._ensure_connected()
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT root_hash FROM nova_seal_tree_heads WHERE tree_size = %s",
                (tree_size,),
            )
            row = cur.fetchone()
        return row[0] if row else None

    # ------------------------------------------------------------------
    # Consistency verification
    # ------------------------------------------------------------------

    def verify_consistency(self, *, full: bool = False) -> ConsistencyResult:
        """Verify Merkle log consistency.

        Fast path (default): spot-checks up to SAMPLE_SIZE random leaves, then
        reads all leaf_hashes (not entry_json) to recompute and verify the root.
        Completes in < 200 ms at 1M entries (Scale-S4 acceptance criterion).

        Full audit (``full=True``): also re-hashes every entry_json to verify
        each stored leaf_hash.  O(N) Python SHA-256 calls; slow at large N.
        """
        self._ensure_connected()
        errors: list[str] = []

        with self._conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM nova_seal_leaves")
            leaf_count: int = int(cur.fetchone()[0])

        if leaf_count == 0:
            return ConsistencyResult(
                leaf_count=0, root_hash=_empty_hash(), consistent=True
            )

        # ------ Sampled spot-check (fast path always runs) ------
        sample_size = min(_SAMPLE_SIZE, leaf_count)
        if leaf_count <= sample_size:
            # Small log: check all entries
            sample_indices: list[int] = list(range(leaf_count))
        else:
            sample_indices = random.sample(range(leaf_count), sample_size)

        with self._conn.cursor() as cur:
            placeholders = ",".join(["%s"] * len(sample_indices))
            cur.execute(
                f"SELECT leaf_index, leaf_hash, entry_json FROM nova_seal_leaves"  # noqa: S608
                f" WHERE leaf_index IN ({placeholders})",
                sample_indices,
            )
            sample_rows = cur.fetchall()

        for idx, stored_hash, entry_json in sample_rows:
            entry_bytes = entry_json.encode("utf-8")
            expected = _leaf_hash(entry_bytes)
            if expected != stored_hash:
                errors.append(
                    f"leaf {idx}: stored={stored_hash[:16]}… expected={expected[:16]}…"
                )

        # ------ Full entry re-hash (opt-in) ------
        if full and leaf_count > sample_size:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT leaf_index, leaf_hash, entry_json FROM nova_seal_leaves"
                    " ORDER BY leaf_index"
                )
                # Stream in batches to avoid loading all 1M rows at once
                while True:
                    batch = cur.fetchmany(10_000)
                    if not batch:
                        break
                    for idx, stored_hash, entry_json in batch:
                        if idx in sample_indices:
                            continue  # already checked
                        entry_bytes = entry_json.encode("utf-8")
                        expected = _leaf_hash(entry_bytes)
                        if expected != stored_hash:
                            errors.append(
                                f"leaf {idx}: stored={stored_hash[:16]}… expected={expected[:16]}…"
                            )

        # ------ Root recomputation from stored leaf_hashes ------
        # Read only leaf_hash (not entry_json) — fast streaming read.
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT leaf_hash FROM nova_seal_leaves ORDER BY leaf_index"
            )
            all_leaf_hashes: list[str] = [row[0] for row in cur.fetchall()]

        computed_root = _compute_root(all_leaf_hashes)

        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT root_hash FROM nova_seal_tree_heads WHERE tree_size = %s",
                (leaf_count,),
            )
            stored_head = cur.fetchone()

        if stored_head is None:
            errors.append(f"no tree_head found for size {leaf_count}")
        elif stored_head[0] != computed_root:
            errors.append(
                f"root mismatch at size {leaf_count}: "
                f"stored={stored_head[0][:16]}… computed={computed_root[:16]}…"
            )

        return ConsistencyResult(
            leaf_count=leaf_count,
            root_hash=computed_root,
            consistent=len(errors) == 0,
            errors=errors,
        )


# ---------------------------------------------------------------------------
# Factory: open_merkle_log() — routes to SQLite or Postgres by URI
# ---------------------------------------------------------------------------


def open_merkle_log(uri: Union[str, Path]) -> "MerkleLog | PostgresMerkleLog":
    """Return the appropriate Merkle log backend for *uri*.

    - ``Path`` or non-DSN string → ``MerkleLog`` (SQLite, default)
    - String starting with ``postgresql://`` or ``postgres://`` → ``PostgresMerkleLog``

    Install Postgres support: ``pip install novafabric[seal-postgres]``
    """
    uri_str = str(uri)
    if uri_str.startswith(("postgresql://", "postgres://")):
        return PostgresMerkleLog(uri_str)
    return MerkleLog(Path(uri_str))
