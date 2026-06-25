"""Tests for TV-5 SnapshotStore3D time-based TTL retention."""
from __future__ import annotations

import asyncio
import time
from pathlib import Path


def _store(tmp_path: Path):
    from novafabric.serve.topology.snapshot_store_3d import SnapshotStore3D

    return SnapshotStore3D(tmp_path / "snapshots")


# ---------------------------------------------------------------------------
# evict_expired — fine tier
# ---------------------------------------------------------------------------


def test_fine_tier_fresh_snapshot_not_evicted(tmp_path):
    """A brand-new snapshot (mtime = now) must not be evicted."""
    store = _store(tmp_path)
    store.save("fresh", {"data": 1})
    evicted_fine, _ = store.evict_expired()
    assert evicted_fine == 0
    assert store.get("fresh") is not None


def test_fine_tier_old_snapshot_evicted(tmp_path):
    """A snapshot older than 24 h must be evicted from the fine tier."""
    store = _store(tmp_path)
    store.save("old_fine", {"data": 2})

    # Back-date the file's mtime by 25 hours
    snap_path = store.fine_dir / "old_fine.snap"
    old_mtime = time.time() - (25 * 3600)
    import os

    os.utime(snap_path, (old_mtime, old_mtime))

    evicted_fine, _ = store.evict_expired()
    assert evicted_fine == 1
    assert store.get("old_fine") is None


def test_fine_tier_exactly_at_boundary_not_evicted(tmp_path):
    """A snapshot slightly inside the 24 h window must be kept."""
    store = _store(tmp_path)
    store.save("boundary_fine", {"data": 3})

    snap_path = store.fine_dir / "boundary_fine.snap"
    # 1 second inside the TTL window (23 h 59 m 59 s old) → must survive
    boundary_mtime = time.time() - (86400 - 1)
    import os

    os.utime(snap_path, (boundary_mtime, boundary_mtime))

    evicted_fine, _ = store.evict_expired()
    assert evicted_fine == 0


# ---------------------------------------------------------------------------
# evict_expired — coarse tier
# ---------------------------------------------------------------------------


def test_coarse_tier_fresh_snapshot_not_evicted(tmp_path):
    """A fresh coarse snapshot must survive eviction."""
    store = _store(tmp_path)
    # Write directly into coarse dir to simulate a coarse snapshot
    path = store.coarse_dir / "coarse_fresh.snap"
    path.write_bytes(store._encode({"data": 10}))  # noqa: SLF001

    _, evicted_coarse = store.evict_expired()
    assert evicted_coarse == 0


def test_coarse_tier_old_snapshot_evicted(tmp_path):
    """A coarse snapshot older than 7 d must be evicted."""
    store = _store(tmp_path)
    path = store.coarse_dir / "coarse_old.snap"
    path.write_bytes(store._encode({"data": 20}))  # noqa: SLF001

    import os

    old_mtime = time.time() - (8 * 24 * 3600)  # 8 days
    os.utime(path, (old_mtime, old_mtime))

    _, evicted_coarse = store.evict_expired()
    assert evicted_coarse == 1


# ---------------------------------------------------------------------------
# evict_expired — mixed
# ---------------------------------------------------------------------------


def test_mixed_tier_eviction_counts(tmp_path):
    """evict_expired returns correct counts for both tiers simultaneously."""
    import os

    store = _store(tmp_path)

    # 2 fresh fine snapshots
    store.save("fine_fresh1", {"d": 1})
    store.save("fine_fresh2", {"d": 2})

    # 1 stale fine snapshot (> 24 h)
    store.save("fine_stale", {"d": 3})
    stale_fine = store.fine_dir / "fine_stale.snap"
    os.utime(stale_fine, (time.time() - 86401, time.time() - 86401))

    # 1 stale coarse snapshot (> 7 d)
    path_coarse = store.coarse_dir / "coarse_stale.snap"
    path_coarse.write_bytes(store._encode({"d": 4}))  # noqa: SLF001
    os.utime(path_coarse, (time.time() - (8 * 86400), time.time() - (8 * 86400)))

    evicted_fine, evicted_coarse = store.evict_expired()
    assert evicted_fine == 1
    assert evicted_coarse == 1


# ---------------------------------------------------------------------------
# start_retention_loop — asyncio task behaviour
# ---------------------------------------------------------------------------


def test_start_retention_loop_calls_evict(tmp_path):
    """start_retention_loop must call evict_expired at least once on first tick."""
    store = _store(tmp_path)
    call_count = [0]
    original_evict = store.evict_expired

    def counting_evict():
        call_count[0] += 1
        return original_evict()

    store.evict_expired = counting_evict  # type: ignore[method-assign]

    async def run():
        task = asyncio.create_task(store.start_retention_loop(interval_seconds=0.01))
        # Let the loop run for a couple of ticks
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(run())
    assert call_count[0] >= 1, "evict_expired was never called by the retention loop"


def test_start_retention_loop_is_cancellable(tmp_path):
    """start_retention_loop must terminate cleanly on task cancellation."""
    store = _store(tmp_path)

    async def run():
        task = asyncio.create_task(store.start_retention_loop(interval_seconds=100))
        await asyncio.sleep(0.01)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass  # expected
        return True

    result = asyncio.run(run())
    assert result


def test_start_retention_loop_continues_after_error(tmp_path):
    """start_retention_loop must not crash if evict_expired raises."""
    store = _store(tmp_path)
    call_count = [0]

    def failing_evict():
        call_count[0] += 1
        if call_count[0] < 3:
            raise RuntimeError("simulated eviction error")
        return (0, 0)

    store.evict_expired = failing_evict  # type: ignore[method-assign]

    async def run():
        task = asyncio.create_task(store.start_retention_loop(interval_seconds=0.01))
        await asyncio.sleep(0.08)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(run())
    # Should have looped at least 3 times despite early failures
    assert call_count[0] >= 3
