"""A read-only Merkle-log open creates no side files (review r8, hole 4).

``GET /api/seal/log/verify`` opens the log with ``mode=ro`` and its docstring says
verification "creates, migrates and writes nothing". The log is in WAL mode, so a
``mode=ro`` connection to a cleanly closed log made SQLite create ``-wal`` and
``-shm`` beside it, and a read-only connection cannot remove them on close.

Now, when neither side file exists — no writer is connected and every transaction is
checkpointed into the main file — the log is opened ``immutable=1``, which takes no
locks and creates nothing. When a side file exists (a writer is connected, or one
exited without checkpointing), ``immutable=1`` would ignore the WAL's committed
leaves and could read pages a concurrent checkpoint is rewriting, so the open stays
``mode=ro`` with normal locking: it reads every committed leaf and adds no file that
was not already there.
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from novafabric.serve.app import create_app
from novafabric.trust.novaseal.merkle import MerkleLog, open_existing_merkle_log

TOKEN = "seal-log-side-files-token"
HOST = {"host": "127.0.0.1:4321"}


def _log(path: Path, n: int = 3) -> None:
    log = MerkleLog(path)
    for i in range(n):
        log.append({"capsule_id": f"{i:064x}", "keyid": "k", "i": i})
    log.close()


def _listing(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir())


def test_a_cleanly_closed_log_is_verified_without_creating_side_files(tmp_path: Path) -> None:
    db = tmp_path / "log.db"
    _log(db)
    before, digest = _listing(tmp_path), hashlib.sha256(db.read_bytes()).hexdigest()
    assert before == ["log.db"], "a closed WAL log should have checkpointed its side files"

    ro = open_existing_merkle_log(db, read_only=True)
    assert ro is not None
    result = ro.verify_consistency()
    assert ro.has_entry_for_capsule_id(f"{1:064x}")
    ro.close()

    assert result.consistent and result.leaf_count == 3
    assert _listing(tmp_path) == before
    assert hashlib.sha256(db.read_bytes()).hexdigest() == digest


def test_a_log_with_a_live_writer_is_read_with_locking_and_sees_wal_only_leaves(
    tmp_path: Path,
) -> None:
    db = tmp_path / "log.db"
    writer = MerkleLog(db)
    try:
        for i in range(4):
            writer.append({"capsule_id": f"{i:064x}", "keyid": "k", "i": i})
        # The writer is connected: its leaves may still be only in the -wal file.
        assert (tmp_path / "log.db-wal").exists()
        before = _listing(tmp_path)

        ro = open_existing_merkle_log(db, read_only=True)
        assert ro is not None
        result = ro.verify_consistency()
        ro.close()

        assert result.consistent and result.leaf_count == 4
        assert _listing(tmp_path) == before
    finally:
        writer.close()


def test_a_read_only_open_never_writes_even_when_immutable(tmp_path: Path) -> None:
    db = tmp_path / "log.db"
    _log(db)
    ro = MerkleLog(db, read_only=True)
    with pytest.raises(sqlite3.OperationalError):
        ro._conn.execute("DELETE FROM leaves")
    ro.close()


def test_the_route_leaves_the_directory_as_it_found_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    logs = tmp_path / "logs"
    logs.mkdir()
    db = logs / "merkle.db"
    _log(db)
    monkeypatch.setenv("NOVAFABRIC_HOME", str(home))
    monkeypatch.setenv("NOVAFABRIC_SEAL_DB_PATH", str(db))
    for name in ("NOVAFABRIC_SEAL_CONFIG", "NOVAFABRIC_CAPSULE_DIR"):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / "runs").mkdir()
    client = TestClient(
        create_app(token=TOKEN, capsule_dir=tmp_path / "runs", db_path=tmp_path / "r.db")
    )

    before = _listing(logs)
    body = client.get("/api/seal/log/verify", params={"token": TOKEN}, headers=HOST).json()

    assert body["consistent"] is True
    assert _listing(logs) == before == ["merkle.db"]
