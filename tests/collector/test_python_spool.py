"""Tests for the Python cffi/pure-Python spool wrapper (B-4).

Integration tests (NOVA_INTEGRATION=1): require libnovaspool.so built from Go.
Unit tests: exercise pure-Python fallback only.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest

from novafabric.collector_cffi.spool import NovaPySpool

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_HEADER_SIZE = 16  # matches collector/internal/spool/segment.go


def _read_segment_payload(path: Path) -> bytes:
    """Skip 16-byte binary header, return payload bytes."""
    raw = path.read_bytes()
    return raw[_HEADER_SIZE:] if len(raw) > _HEADER_SIZE else raw


def _count_events(spool_dir: Path) -> int:
    """Count total non-empty JSON payloads across all committed .jsonl files."""
    total = 0
    for f in sorted(spool_dir.glob("*.jsonl")):
        payload = _read_segment_payload(f)
        for line in payload.splitlines():
            if line.strip():
                total += 1
    return total


# ---------------------------------------------------------------------------
# Unit tests — pure-Python fallback (no libnovaspool.so needed)
# ---------------------------------------------------------------------------

class TestNovaPySpoolPurePython:
    """All tests use the pure-Python fallback (no .so present in CI)."""

    def test_init_creates_spool_dir(self, tmp_path: Path) -> None:
        spool = NovaPySpool(tmp_path / "spool")
        assert (tmp_path / "spool").is_dir()
        assert not spool.using_cffi

    def test_write_bytes_creates_jsonl_file(self, tmp_path: Path) -> None:
        spool = NovaPySpool(tmp_path)
        spool.write(b'{"event_type": "run.start"}')
        jsonl_files = list(tmp_path.glob("*.jsonl"))
        assert len(jsonl_files) >= 1

    def test_write_str_accepted(self, tmp_path: Path) -> None:
        spool = NovaPySpool(tmp_path)
        spool.write('{"event_type": "run.end"}')
        assert _count_events(tmp_path) == 1

    def test_multiple_writes_all_committed(self, tmp_path: Path) -> None:
        spool = NovaPySpool(tmp_path)
        for i in range(10):
            spool.write(json.dumps({"seq": i}).encode())
        assert _count_events(tmp_path) == 10

    def test_write_is_valid_json(self, tmp_path: Path) -> None:
        spool = NovaPySpool(tmp_path)
        payload = {"event_type": "tool.call", "run_id": "r-001"}
        spool.write(json.dumps(payload))
        # Skip 16-byte binary header, then parse the JSON payload line.
        for f in tmp_path.glob("*.jsonl"):
            body = _read_segment_payload(f)
            for line in body.splitlines():
                if line.strip():
                    parsed = json.loads(line.strip())
                    assert parsed["event_type"] == "tool.call"

    def test_drain_does_not_raise(self, tmp_path: Path) -> None:
        spool = NovaPySpool(tmp_path)
        spool.write(b'{"x": 1}')
        spool.drain()  # must not raise

    def test_close_does_not_raise(self, tmp_path: Path) -> None:
        spool = NovaPySpool(tmp_path)
        spool.close()

    def test_thread_safety_1000_events(self, tmp_path: Path) -> None:
        """10 threads × 100 events each = 1000 total — no data loss or corruption."""
        spool = NovaPySpool(tmp_path, max_files=2000)
        errors: list[Exception] = []

        def writer(thread_id: int) -> None:
            try:
                for seq in range(100):
                    spool.write(json.dumps({"thread": thread_id, "seq": seq}))
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Thread errors: {errors}"
        assert _count_events(tmp_path) == 1000

    def test_no_partial_writes_in_jsonl(self, tmp_path: Path) -> None:
        """Every payload line in every .jsonl file must be valid JSON."""
        spool = NovaPySpool(tmp_path)
        for i in range(20):
            spool.write(json.dumps({"i": i}))
        for f in sorted(tmp_path.glob("*.jsonl")):
            body = _read_segment_payload(f)
            for line in body.splitlines():
                if line.strip():
                    json.loads(line)  # must not raise

    def test_eviction_removes_oldest_files(self, tmp_path: Path) -> None:
        """When max_files is exceeded, oldest committed files are evicted."""
        spool = NovaPySpool(tmp_path, max_files=3)
        # Write enough events that each write creates a new file (use unique timestamps).
        # We force unique filenames by sleeping briefly between writes.
        for _ in range(6):
            spool.write(b'{"x": 1}')
            time.sleep(0.011)  # ensure unique ms-resolution filename
        # Eviction runs before each write, so after N writes with max_files=3
        # the retained count is at most max_files+1 (the just-written file).
        committed = sorted(tmp_path.glob("*.jsonl"))
        assert len(committed) <= 4, f"Expected ≤4 files, got {len(committed)}: {committed}"
        assert len(committed) >= 3, "Expect at least max_files files retained"

    def test_key_rotation_new_spool_dir(self, tmp_path: Path) -> None:
        """Creating a new NovaPySpool with a different dir mid-stream loses nothing."""
        dir_a = tmp_path / "spool-a"
        dir_b = tmp_path / "spool-b"
        spool_a = NovaPySpool(dir_a)
        spool_a.write(b'{"stage": "before"}')
        # 'Rotate' — create a new spool for the new key epoch.
        spool_b = NovaPySpool(dir_b)
        spool_b.write(b'{"stage": "after"}')
        assert _count_events(dir_a) == 1
        assert _count_events(dir_b) == 1


# ---------------------------------------------------------------------------
# Integration tests — NOVA_INTEGRATION=1 gate
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    not os.environ.get("NOVA_INTEGRATION"),
    reason="Requires NOVA_INTEGRATION=1 and libnovaspool.so built from Go",
)
class TestNovaPySpoolCffiIntegration:
    def test_cffi_backend_used_when_so_present(self, tmp_path: Path) -> None:
        """When libnovaspool.so is present, cffi backend is active."""
        spool = NovaPySpool(tmp_path)
        # In a real integration run with .so present, this must be True.
        assert spool.using_cffi, "Expected cffi backend but pure-Python was selected"

    def test_cffi_write_roundtrip(self, tmp_path: Path) -> None:
        spool = NovaPySpool(tmp_path)
        spool.write(b'{"event_type": "cffi.test"}')
        assert _count_events(tmp_path) == 1
