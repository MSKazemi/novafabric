"""Tests for PostgresMerkleLog and open_merkle_log() factory — Scale-S4.

Unit tests run without Postgres. Integration + benchmark tests require:
    NOVA_INTEGRATION=1
    NOVA_TEST_POSTGRES_DSN=postgresql://postgres:postgres@localhost:5432/nova_test
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

INTEGRATION = os.environ.get("NOVA_INTEGRATION") == "1"
PG_DSN = os.environ.get("NOVA_TEST_POSTGRES_DSN", "")
SKIP_INTEGRATION = pytest.mark.skipif(
    not (INTEGRATION and PG_DSN),
    reason="Set NOVA_INTEGRATION=1 and NOVA_TEST_POSTGRES_DSN to run Postgres tests",
)
psycopg = pytest.importorskip("psycopg", reason="psycopg not installed; pip install novafabric[seal-postgres]")


# ---------------------------------------------------------------------------
# Unit: open_merkle_log() factory routing
# ---------------------------------------------------------------------------


class TestOpenMerkleLogFactory:
    def test_sqlite_path_returns_sqlite_log(self, tmp_path):
        from novafabric.trust.novaseal.merkle import MerkleLog, open_merkle_log

        log = open_merkle_log(tmp_path / "test.db")
        assert isinstance(log, MerkleLog)
        log.close()

    def test_sqlite_string_path_returns_sqlite_log(self, tmp_path):
        from novafabric.trust.novaseal.merkle import MerkleLog, open_merkle_log

        log = open_merkle_log(str(tmp_path / "test2.db"))
        assert isinstance(log, MerkleLog)
        log.close()

    def test_postgres_dsn_returns_postgres_log(self):
        from novafabric.trust.novaseal.merkle import PostgresMerkleLog, open_merkle_log

        # open_merkle_log detects the prefix and returns PostgresMerkleLog WITHOUT
        # actually connecting (lazy connect on first use).
        log = open_merkle_log("postgresql://localhost/test")
        assert isinstance(log, PostgresMerkleLog)

    def test_postgres_dsn_prefix_variant(self):
        from novafabric.trust.novaseal.merkle import PostgresMerkleLog, open_merkle_log

        log = open_merkle_log("postgres://localhost/test")
        assert isinstance(log, PostgresMerkleLog)


# ---------------------------------------------------------------------------
# Unit: resolve_merkle_db_uri() — supports DSNs as well as paths
# ---------------------------------------------------------------------------


class TestResolveMerkleDbUri:
    def test_env_var_sqlite_path(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NOVAFABRIC_SEAL_DB_PATH", str(tmp_path / "seal.db"))
        from novafabric.trust.novaseal.config import resolve_merkle_db_uri

        uri = resolve_merkle_db_uri()
        assert uri == str(tmp_path / "seal.db")

    def test_env_var_postgres_dsn(self, monkeypatch):
        monkeypatch.setenv("NOVAFABRIC_SEAL_DB_PATH", "postgresql://localhost:5432/nova")
        from novafabric.trust.novaseal.config import resolve_merkle_db_uri

        uri = resolve_merkle_db_uri()
        assert uri == "postgresql://localhost:5432/nova"

    def test_returns_string_not_path(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NOVAFABRIC_SEAL_DB_PATH", str(tmp_path / "seal.db"))
        from novafabric.trust.novaseal.config import resolve_merkle_db_uri

        uri = resolve_merkle_db_uri()
        assert isinstance(uri, str)

    def test_default_returns_string(self, monkeypatch):
        monkeypatch.delenv("NOVAFABRIC_SEAL_DB_PATH", raising=False)
        monkeypatch.delenv("NOVAFABRIC_SEAL_CONFIG", raising=False)
        from novafabric.trust.novaseal.config import resolve_merkle_db_uri

        uri = resolve_merkle_db_uri()
        assert isinstance(uri, str)

    def test_resolve_merkle_db_path_still_works(self, monkeypatch, tmp_path):
        """Backward-compat: resolve_merkle_db_path() still returns Path for file paths."""
        monkeypatch.setenv("NOVAFABRIC_SEAL_DB_PATH", str(tmp_path / "seal.db"))
        from novafabric.trust.novaseal.config import resolve_merkle_db_path

        p = resolve_merkle_db_path()
        assert isinstance(p, Path)


# ---------------------------------------------------------------------------
# Unit: PostgresMerkleLog constructor — lazy connect, no real DB needed
# ---------------------------------------------------------------------------


class TestPostgresMerkleLogUnit:
    def test_dsn_stored(self):
        from novafabric.trust.novaseal.merkle import PostgresMerkleLog

        log = PostgresMerkleLog("postgresql://localhost/test")
        assert log._dsn == "postgresql://localhost/test"

    def test_close_before_connect_is_safe(self):
        from novafabric.trust.novaseal.merkle import PostgresMerkleLog

        log = PostgresMerkleLog("postgresql://localhost/test")
        log.close()  # no connection yet — must not raise


# ---------------------------------------------------------------------------
# Integration: real Postgres operations
# ---------------------------------------------------------------------------


@SKIP_INTEGRATION
class TestPostgresMerkleLogIntegration:
    """Requires a live Postgres instance. Skip without NOVA_INTEGRATION=1."""

    @pytest.fixture(autouse=True)
    def _clean_table(self):
        """Drop and re-create Merkle log tables before each test."""
        conn = psycopg.connect(PG_DSN)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS nova_seal_leaves CASCADE")
            cur.execute("DROP TABLE IF EXISTS nova_seal_tree_heads CASCADE")
            cur.execute("DROP SEQUENCE IF EXISTS nova_seal_leaf_seq CASCADE")
        conn.close()
        yield

    def test_append_and_read_leaf(self):
        from novafabric.trust.novaseal.merkle import PostgresMerkleLog

        log = PostgresMerkleLog(PG_DSN)
        entry = log.append({"capsule_id": "cap-pg-001", "has_tsr": False})
        assert entry["leaf_index"] == 0
        assert len(entry["leaf_hash"]) == 64
        assert entry["tree_size"] == 1

        leaf = log.get_leaf(0)
        assert leaf is not None
        assert leaf["capsule_id"] == "cap-pg-001"
        log.close()

    def test_tree_size(self):
        from novafabric.trust.novaseal.merkle import PostgresMerkleLog

        log = PostgresMerkleLog(PG_DSN)
        assert log.tree_size() == 0
        log.append({"capsule_id": "x"})
        assert log.tree_size() == 1
        log.append({"capsule_id": "y"})
        assert log.tree_size() == 2
        log.close()

    def test_current_root_none_when_empty(self):
        from novafabric.trust.novaseal.merkle import PostgresMerkleLog

        log = PostgresMerkleLog(PG_DSN)
        assert log.current_root() is None
        log.close()

    def test_current_root_set_after_append(self):
        from novafabric.trust.novaseal.merkle import PostgresMerkleLog

        log = PostgresMerkleLog(PG_DSN)
        result = log.append({"capsule_id": "a"})
        assert log.current_root() == result["root_hash"]
        log.close()

    def test_verify_entry_returns_true(self):
        from novafabric.trust.novaseal.merkle import PostgresMerkleLog

        log = PostgresMerkleLog(PG_DSN)
        for i in range(5):
            log.append({"index": i})
        assert log.verify_entry(0) is True
        assert log.verify_entry(4) is True
        log.close()

    def test_verify_consistency_empty(self):
        from novafabric.trust.novaseal.merkle import PostgresMerkleLog

        log = PostgresMerkleLog(PG_DSN)
        result = log.verify_consistency()
        assert result.consistent is True
        assert result.leaf_count == 0
        log.close()

    def test_verify_consistency_100_entries(self):
        from novafabric.trust.novaseal.merkle import PostgresMerkleLog

        log = PostgresMerkleLog(PG_DSN)
        for i in range(100):
            log.append({"i": i, "data": f"entry-{i}"})
        result = log.verify_consistency()
        assert result.consistent is True
        assert result.leaf_count == 100
        assert result.errors == []
        log.close()

    def test_inclusion_proof_valid(self):
        from novafabric.trust.novaseal.merkle import PostgresMerkleLog, verify_inclusion_proof

        log = PostgresMerkleLog(PG_DSN)
        for i in range(8):
            log.append({"i": i})
        root = log.current_root()
        assert root is not None
        proof = log.get_inclusion_proof(3)
        leaf_hash = log.get_leaf_hash(3)
        assert verify_inclusion_proof(leaf_hash, 3, proof, root, log.tree_size())
        log.close()


# ---------------------------------------------------------------------------
# Integration benchmark: 1M entries, verify_consistency < 200ms p99
# ---------------------------------------------------------------------------


@SKIP_INTEGRATION
class TestPostgresMerkleLogBenchmark:
    @pytest.fixture(autouse=True)
    def _clean_table(self):
        conn = psycopg.connect(PG_DSN)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS nova_seal_leaves CASCADE")
            cur.execute("DROP TABLE IF EXISTS nova_seal_tree_heads CASCADE")
            cur.execute("DROP SEQUENCE IF EXISTS nova_seal_leaf_seq CASCADE")
        conn.close()
        yield

    def test_verify_consistency_1m_entries_regression_baseline(self):
        """Regression guard on `verify_consistency()` cost at 1M log entries.

        **This is not the Scale-S4 acceptance criterion, and deliberately no
        longer claims to be.** As written this asserted `p99 < 200 ms`, failed on
        every run it ever made, and was red nightly from 2026-08-05 while
        `docs/releases/v0.38.0.md` told readers the criterion was met.

        Two things were wrong with it:

        1. **The threshold is unreachable, not merely missed.** The default path
           recomputes the Merkle root from all 1M stored leaf hashes — ~1M Python
           SHA-256 calls, measured at 1361 ms in-process with no database
           involved, 6.8x the whole budget. Faster hardware cannot close that.
        2. **`max()` of 5 samples is not a p99.** With n=5 the maximum estimates
           roughly the 83rd percentile. The statistic was misnamed independently
           of what threshold it was compared against.

        So this now measures the worst of N runs and guards against *regression*
        past a measured ceiling, which is a claim the test can actually support.
        Restoring a real 200 ms gate means changing the algorithm — verifying
        inclusion proofs against a signed tree head rather than recomputing the
        full root. That is a weaker guarantee, so it needs an ADR, not a
        quietly-relaxed number here.
        """
        from novafabric.trust.novaseal.merkle import PostgresMerkleLog

        log = PostgresMerkleLog(PG_DSN)

        # Bulk-insert 1M entries using execute_many for speed
        log._ensure_connected()
        TOTAL = 1_000_000
        import json

        with log._conn.transaction():
            # Pre-generate batch
            rows = [
                (
                    i,
                    __import__("hashlib").sha256(
                        b"\x00" + json.dumps({"i": i}, sort_keys=True, separators=(",", ":")).encode()
                    ).hexdigest(),
                    json.dumps({"i": i}, sort_keys=True, separators=(",", ":")),
                )
                for i in range(TOTAL)
            ]
            # Insert root for total size
            leaf_hashes = [r[1] for r in rows]
            root = _compute_root_for_benchmark(leaf_hashes)
            with log._conn.cursor() as cur:
                # copy_rows is psycopg3 fast path
                cur.executemany(
                    "INSERT INTO nova_seal_leaves (leaf_index, leaf_hash, entry_json) VALUES (%s, %s, %s)",
                    rows,
                )
                cur.execute(
                    "INSERT INTO nova_seal_tree_heads (tree_size, root_hash) VALUES (%s, %s)",
                    (TOTAL, root),
                )

        # Worst of N runs, reported as exactly that. The ceiling is a regression
        # bound with headroom over the ~1.9 s this measures on a GitHub-hosted
        # runner — it is NOT an acceptance target, and tightening it toward
        # 200 ms is a code change, not a number change.
        ROUNDS = 5
        REGRESSION_CEILING_MS = 5_000

        times = []
        for _ in range(ROUNDS):
            t0 = time.perf_counter()
            result = log.verify_consistency()
            times.append(time.perf_counter() - t0)

        log.close()

        worst_ms = max(times) * 1000
        assert result.leaf_count == TOTAL
        assert result.consistent is True
        assert worst_ms < REGRESSION_CEILING_MS, (
            f"worst of {ROUNDS} verify_consistency() runs at {TOTAL:,} entries "
            f"was {worst_ms:.1f}ms, past the {REGRESSION_CEILING_MS}ms "
            f"regression ceiling (all runs: "
            f"{', '.join(f'{t * 1000:.0f}ms' for t in times)}). This guards "
            f"against a slowdown, not against the withdrawn 200ms target."
        )


def _compute_root_for_benchmark(leaf_hashes: list[str]) -> str:
    """Helper: compute Merkle root for benchmark setup."""
    from novafabric.trust.novaseal.merkle import _compute_root

    return _compute_root(leaf_hashes)
