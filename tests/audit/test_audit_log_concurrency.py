"""Concurrent appends never fork the hash chain (review r8, hole 1).

An adversarial review ran 8 threads x 5 ``put_bypass`` calls against one log and got
40 bypasses with 12 ``prev_hash`` mismatches: each ``AuditLog`` read the last hash
when it was constructed and appended without a lock, so two writers chained onto the
same predecessor. ``append`` now reads the tail and writes under an exclusive lock
held across threads (in-process lock) and processes (``fcntl.flock``).
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from novafabric.audit import AuditEventType, AuditLog
from novafabric.audit import _log as audit_log_module

THREADS = 8
PER_THREAD = 6


def _seed(path: Path, n: int = 50) -> None:
    log = AuditLog(path)
    for i in range(n):
        log.append(AuditEventType.PROMOTE, "seed", f"r{i}", {"pad": "x" * 200})


def _lines(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_threads_each_with_their_own_instance_keep_the_chain(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    _seed(path)
    barrier = threading.Barrier(THREADS)

    def worker(i: int) -> None:
        barrier.wait()
        for j in range(PER_THREAD):
            # A fresh instance per append, as every production writer does.
            AuditLog(path).append(AuditEventType.SEAL_BYPASS_CREATE, f"t{i}", f"c{i}-{j}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert AuditLog(path).verify() == []
    assert len(_lines(path)) == 50 + THREADS * PER_THREAD


def test_one_shared_long_lived_instance_keeps_the_chain(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    shared = AuditLog(path)
    barrier = threading.Barrier(THREADS)

    def worker(i: int) -> None:
        barrier.wait()
        for j in range(PER_THREAD):
            shared.append(AuditEventType.ALERT_DELIVERY, f"t{i}", f"c{i}-{j}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert shared.verify() == []


def test_a_long_lived_instance_chains_onto_entries_another_writer_appended(
    tmp_path: Path,
) -> None:
    path = tmp_path / "audit.jsonl"
    early = AuditLog(path)  # constructed before the other writer appends
    early.append(AuditEventType.PROMOTE, "a", "r1")
    AuditLog(path).append(AuditEventType.PROMOTE, "b", "r2")
    early.append(AuditEventType.PROMOTE, "a", "r3")

    assert AuditLog(path).verify() == []



def test_threads_stay_serialised_without_flock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The in-process lock alone holds the chain (no ``fcntl``, or one shared fd)."""
    monkeypatch.setattr(audit_log_module, "fcntl", None)
    path = tmp_path / "audit.jsonl"
    barrier = threading.Barrier(THREADS)

    def worker(i: int) -> None:
        barrier.wait()
        for j in range(PER_THREAD):
            AuditLog(path).append(AuditEventType.PROMOTE, f"t{i}", f"c{i}-{j}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert AuditLog(path).verify() == []

_CHILD = """
import sys
from pathlib import Path
from novafabric.audit import AuditEventType, AuditLog
path, tag, n = Path(sys.argv[1]), sys.argv[2], int(sys.argv[3])
for j in range(n):
    AuditLog(path).append(AuditEventType.SEAL_BYPASS_CREATE, tag, f"{tag}-{j}")
"""


def test_processes_appending_concurrently_keep_the_chain(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    _seed(path)
    procs = [
        subprocess.Popen(  # noqa: S603 — fixed interpreter and an inline script
            [sys.executable, "-c", _CHILD, str(path), f"p{i}", "15"],
        )
        for i in range(4)
    ]
    for p in procs:
        assert p.wait(timeout=120) == 0

    assert AuditLog(path).verify() == []
    assert len(_lines(path)) == 50 + 4 * 15


def test_last_hash_lookup_reads_only_the_tail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A big log is not re-read in full on every append."""
    path = tmp_path / "audit.jsonl"
    _seed(path, n=2000)  # ~0.7 MB
    size = path.stat().st_size
    read_bytes = 0
    real = audit_log_module._read_block

    def counting(fd: int, offset: int, length: int) -> bytes:
        nonlocal read_bytes
        data = real(fd, offset, length)
        read_bytes += len(data)
        return data

    monkeypatch.setattr(audit_log_module, "_read_block", counting)
    AuditLog(path).append(AuditEventType.PROMOTE, "x", "y")

    assert read_bytes < size // 10
    assert AuditLog(path).verify() == []


def test_a_torn_last_line_is_terminated_not_merged_into(tmp_path: Path) -> None:
    """A crash mid-write leaves a line with no newline; the next entry starts afresh."""
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    first = log.append(AuditEventType.PROMOTE, "a", "r1")
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"entry_hash":"torn')  # no newline: a torn write
    second = AuditLog(path).append(AuditEventType.PROMOTE, "b", "r2")

    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[1] == '{"entry_hash":"torn'
    assert json.loads(lines[2])["entry_id"] == second.entry_id
    # It chains onto the last *valid* entry, as the full replay always did.
    assert second.prev_hash == first.entry_hash
