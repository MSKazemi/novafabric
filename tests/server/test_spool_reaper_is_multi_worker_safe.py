"""The spool reaper must not eat a sibling worker's in-flight upload.

Follow-up to defect **B9**. The reaper that reclaims orphaned ingest temporaries
originally decided ownership by a single test: *is this entry older than this
process's start time?* On one process that is exact. Under ``--workers N`` it is
not.

`cli/server.py::_launch_workers` runs uvicorn with `factory=True, workers=N`, so
**every worker process runs the app lifespan and therefore its own reaper, with
its own start time** — and uvicorn respawns a worker that dies. A worker
respawned at T sees every sibling's in-flight spool (created before T) as
predating "the current server" and deletes it: the streaming `.spool` file and
the half-extracted `<run_id>.<hex>` directory both.

Measured before the fix: a reaper given a start time 50 ms after two in-flight
entries removed both of them.

The reaper now requires an entry to be older than **both** the process start
time and `now - SPOOL_REAP_GRACE_S`. The grace term is the half that holds in a
multi-process deployment. This is the same family as B8 — an invariant stated
for one process that does not survive `--workers > 1`.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from novafabric.server.ingest import SPOOL_REAP_GRACE_S, reap_orphaned_spools


def _inflight(spool: Path) -> tuple[Path, Path]:
    """The two transient kinds a live upload owns."""
    spool_file = spool / "abc123.spool"
    spool_file.write_bytes(b"x" * 64)
    extract_dir = spool / "01RUN.deadbeef"
    extract_dir.mkdir()
    (extract_dir / "part").write_bytes(b"y" * 16)
    return spool_file, extract_dir


def _age(path: Path, seconds: float) -> None:
    stamp = time.time() - seconds
    os.utime(path, (stamp, stamp))


class TestARespawnedWorkerLeavesSiblingsAlone:
    def test_inflight_entries_survive_a_later_started_worker(
        self, tmp_path: Path
    ) -> None:
        spool_file, extract_dir = _inflight(tmp_path)
        # uvicorn respawns a worker now: strictly after the sibling's entries.
        time.sleep(0.05)
        removed = reap_orphaned_spools(tmp_path, started_at=time.time())
        assert removed == 0, "a respawned worker reaped a sibling's upload"
        assert spool_file.exists(), "B9 follow-up: in-flight .spool deleted"
        assert extract_dir.exists(), (
            "B9 follow-up: half-extracted directory deleted mid-upload"
        )

    def test_an_upload_just_under_the_grace_period_survives(
        self, tmp_path: Path
    ) -> None:
        spool_file, extract_dir = _inflight(tmp_path)
        _age(spool_file, SPOOL_REAP_GRACE_S * 0.5)
        _age(extract_dir, SPOOL_REAP_GRACE_S * 0.5)
        assert reap_orphaned_spools(tmp_path, started_at=time.time()) == 0
        assert spool_file.exists() and extract_dir.exists()


class TestGenuineOrphansAreStillReclaimed:
    """Guard the guard: a reaper that never reaps would pass everything above."""

    def test_week_old_entries_are_removed(self, tmp_path: Path) -> None:
        spool_file, extract_dir = _inflight(tmp_path)
        _age(spool_file, 7 * 24 * 3600)
        _age(extract_dir, 7 * 24 * 3600)
        removed = reap_orphaned_spools(tmp_path, started_at=time.time())
        assert removed == 2, f"expected both transients reclaimed, got {removed}"
        assert not spool_file.exists()
        assert not extract_dir.exists(), (
            "reaping only the file was the first cut and still leaks — a live "
            "crash run reclaimed a 201 MB spool and left a 28 MB directory"
        )

    def test_unrelated_files_are_never_touched(self, tmp_path: Path) -> None:
        keeper = tmp_path / "not-a-spool.txt"
        keeper.write_bytes(b"z")
        _age(keeper, 7 * 24 * 3600)
        assert reap_orphaned_spools(tmp_path, started_at=time.time()) == 0
        assert keeper.exists()


class TestReclamationNeverBlocksStartup:
    def test_missing_spool_dir_returns_zero(self, tmp_path: Path) -> None:
        assert reap_orphaned_spools(tmp_path / "absent", started_at=time.time()) == 0
