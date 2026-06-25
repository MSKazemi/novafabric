"""Tests for WAL-backed CRDTAccumulator.

Tests crash recovery, sentinel writing, WAL replay, and flush behaviour.
No network or database dependencies — all tests run in a tmp_path directory.
"""
from __future__ import annotations

from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_accumulator(wal_dir: Path, filename: str = "test.wal") -> object:
    from novafabric.kg.crdt import CRDTAccumulator

    return CRDTAccumulator(wal_path=wal_dir / filename)


# ---------------------------------------------------------------------------
# Basic WAL behaviour
# ---------------------------------------------------------------------------


def test_wal_created_on_first_accumulate(tmp_path: Path) -> None:
    """WAL file is created when the first event is accumulated."""
    acc = _make_accumulator(tmp_path)
    acc.accumulate("agent-1", "gpt-4o", "CALLS", "cap-1")  # type: ignore[attr-defined]
    wal_file = tmp_path / "test.wal"
    assert wal_file.exists()
    assert wal_file.stat().st_size > 0


def test_wal_flush_writes_sentinel(tmp_path: Path) -> None:
    """flush() writes FLUSH_CONFIRMED sentinel to the WAL."""
    from novafabric.kg.crdt import CRDTAccumulator

    acc = CRDTAccumulator(wal_path=tmp_path / "test.wal")
    acc.accumulate("a", "b", "CALLS", "cap-1")
    acc.flush()
    wal_bytes = (tmp_path / "test.wal").read_bytes()
    assert b"FLUSH_CONFIRMED" in wal_bytes


def test_wal_flush_resets_counters(tmp_path: Path) -> None:
    """After flush(), len(accumulator) == 0."""
    acc = _make_accumulator(tmp_path)
    acc.accumulate("a", "b", "CALLS", "cap-1")  # type: ignore[attr-defined]
    assert len(acc) == 1  # type: ignore[arg-type]
    acc.flush()  # type: ignore[attr-defined]
    assert len(acc) == 0  # type: ignore[arg-type]


def test_wal_accumulate_without_wal_path_still_works() -> None:
    """CRDTAccumulator without WAL path behaves as before (in-memory only)."""
    from novafabric.kg.crdt import CRDTAccumulator

    acc = CRDTAccumulator()
    acc.accumulate("a", "b", "CALLS", "cap-1", novaseal_valid=True)
    deltas = acc.flush()
    assert len(deltas) == 1
    assert deltas[0]["call_count"] == 1


# ---------------------------------------------------------------------------
# Crash recovery
# ---------------------------------------------------------------------------


def test_wal_recovery_replays_pending_increments(tmp_path: Path) -> None:
    """After a simulated crash (no FLUSH_CONFIRMED), WAL is replayed on init."""
    from novafabric.kg.crdt import CRDTAccumulator

    wal_path = tmp_path / "crash.wal"

    # Phase 1: accumulate 3 events, then "crash" (no flush)
    acc1 = CRDTAccumulator(wal_path=wal_path)
    acc1.accumulate("agent-1", "gpt-4o", "CALLS", "cap-1", novaseal_valid=True)
    acc1.accumulate("agent-1", "gpt-4o", "CALLS", "cap-2", novaseal_valid=False)
    acc1.accumulate("agent-2", "gpt-4o", "CALLS", "cap-3", novaseal_valid=True)
    # Crash — no flush()

    # Phase 2: new accumulator opens same WAL — should replay
    acc2 = CRDTAccumulator(wal_path=wal_path)
    assert len(acc2) == 3  # type: ignore[arg-type]
    deltas = acc2.flush()
    found = {(str(d["src"]), str(d["dst"])): int(d["call_count"]) for d in deltas}
    assert found[("agent-1", "gpt-4o")] == 2
    assert found[("agent-2", "gpt-4o")] == 1


def test_wal_recovery_skips_before_last_sentinel(tmp_path: Path) -> None:
    """Increments before the last FLUSH_CONFIRMED are NOT replayed."""
    from novafabric.kg.crdt import CRDTAccumulator

    wal_path = tmp_path / "sentinel.wal"

    # Phase 1: accumulate + flush (sentinel written)
    acc1 = CRDTAccumulator(wal_path=wal_path)
    acc1.accumulate("a", "b", "CALLS", "cap-1")
    acc1.flush()  # writes sentinel

    # Phase 2: accumulate 2 more events, then crash
    acc2 = CRDTAccumulator(wal_path=wal_path)
    # After recovery acc2 should be empty (pre-sentinel was flushed)
    # But we haven't added new events yet — len should be 0
    assert len(acc2) == 0  # type: ignore[arg-type]

    # Now add new events (these will be in WAL after the sentinel)
    acc2.accumulate("a", "b", "CALLS", "cap-2")
    acc2.accumulate("a", "b", "CALLS", "cap-3")
    # Simulate crash (no flush)

    # Phase 3: recovery — should replay only the 2 new events
    acc3 = CRDTAccumulator(wal_path=wal_path)
    assert len(acc3) == 2  # type: ignore[arg-type]


def test_wal_recovery_with_empty_wal(tmp_path: Path) -> None:
    """Empty WAL file is handled gracefully — accumulator starts empty."""
    wal_path = tmp_path / "empty.wal"
    wal_path.write_bytes(b"")  # empty file
    from novafabric.kg.crdt import CRDTAccumulator

    acc = CRDTAccumulator(wal_path=wal_path)
    assert len(acc) == 0  # type: ignore[arg-type]


def test_wal_recovery_with_corrupted_line(tmp_path: Path) -> None:
    """Corrupted JSON lines in WAL are skipped gracefully."""
    wal_path = tmp_path / "corrupt.wal"
    # Write one valid line, one corrupt line, one valid line
    import json as _json

    valid = _json.dumps({"k": ["a", "b", "CALLS"], "cid": "cap-1", "v": 0}).encode()
    corrupt = b"NOT_JSON_AT_ALL\n"
    valid2 = _json.dumps({"k": ["a", "c", "USES_TOOL"], "cid": "cap-2", "v": 1}).encode()
    wal_path.write_bytes(valid + b"\n" + corrupt + valid2 + b"\n")

    from novafabric.kg.crdt import CRDTAccumulator

    acc = CRDTAccumulator(wal_path=wal_path)
    # Two valid entries should be replayed
    assert len(acc) == 2  # type: ignore[arg-type]


def test_wal_multiple_flush_cycles(tmp_path: Path) -> None:
    """Multiple flush cycles do not accumulate stale data from previous cycles."""
    from novafabric.kg.crdt import CRDTAccumulator

    wal_path = tmp_path / "multi.wal"

    acc = CRDTAccumulator(wal_path=wal_path)
    acc.accumulate("a", "b", "CALLS", "cap-1")
    acc.flush()

    acc.accumulate("a", "b", "CALLS", "cap-2")
    deltas = acc.flush()

    # Only the second cycle's event should appear
    assert len(deltas) == 1
    assert deltas[0]["call_count"] == 1
