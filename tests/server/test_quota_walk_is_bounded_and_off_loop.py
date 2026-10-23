"""M6 — the storage-quota recount must not stall ingest or count in-flight uploads.

``enforce_storage_quota`` is an ``async`` FastAPI dependency; it called
``QuotaChecker.check()`` → ``measure_capsule_store`` — a synchronous ``os.walk``
of the WHOLE capsule store — directly on the event loop (at most once per TTL),
so every request on that worker waited for the walk. The walk also counted
``<capsule_dir>/.ingest-tmp/`` — the spool holding uploads still being received
and extracted — so a large in-flight upload could push ``total_bytes`` over the
hard limit and 429 unrelated writes. These tests pin:

* the spool is excluded from both the capsule count and the byte count;
* symlinks are sized by the link, never by their target (``lstat``);
* the walk is bounded by an entry budget; a truncated walk reports a LOWER
  bound, which can still reject (never an over-count);
* the dependency runs the recount on a worker thread, not the event loop;
* while one recount is in flight, other requests use the last value instead of
  queueing behind the store lock (single-flight, bounded staleness).
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("fastapi")

from novafabric.server.config import QuotaConfig  # noqa: E402
from novafabric.server.ingest import SPOOL_DIR_NAME  # noqa: E402
from novafabric.server.quotas import (  # noqa: E402
    QuotaChecker,
    QuotaExceededError,
    QuotaUsage,
    enforce_storage_quota,
    measure_capsule_store,
)


def _capsule(root: Path, name: str, payload: int) -> None:
    d = root / name
    d.mkdir(parents=True)
    (d / "capsule.yaml").write_bytes(b"x" * 10)
    (d / "events.jsonl").write_bytes(b"y" * payload)


# --------------------------------------------------------------------------- #
# What is counted
# --------------------------------------------------------------------------- #


def test_spool_is_excluded_from_bytes_and_capsules(tmp_path: Path) -> None:
    _capsule(tmp_path, "run-1", 100)
    spool = tmp_path / SPOOL_DIR_NAME / "upload-abc"
    spool.mkdir(parents=True)
    (spool / "body.zip").write_bytes(b"z" * 50_000)
    # An extracted-but-unpublished capsule in the spool must not count either.
    (spool / "capsule.yaml").write_bytes(b"x" * 10)
    usage = measure_capsule_store(tmp_path)
    assert usage == QuotaUsage(capsules=1, total_bytes=110)


def test_symlinks_are_sized_by_the_link_not_the_target(tmp_path: Path) -> None:
    store = tmp_path / "store"
    _capsule(store, "run-1", 100)
    outside = tmp_path / "big.bin"
    outside.write_bytes(b"b" * 1_000_000)
    os.symlink(outside, store / "run-1" / "link.bin")
    usage = measure_capsule_store(store)
    assert usage.total_bytes < 1_000_000
    assert usage.total_bytes == 110 + os.lstat(store / "run-1" / "link.bin").st_size


def test_walk_is_bounded_and_reports_a_lower_bound(tmp_path: Path) -> None:
    for i in range(10):
        _capsule(tmp_path, f"run-{i}", 100)
    full = measure_capsule_store(tmp_path)
    assert full.truncated is False
    # 10 top-level dirs + 20 files = 30 entries; 15 sees the dirs and some files.
    bounded = measure_capsule_store(tmp_path, max_entries=15)
    assert bounded.truncated is True
    assert 0 < bounded.total_bytes < full.total_bytes  # a lower bound, never more


def test_a_truncated_lower_bound_over_the_hard_limit_still_rejects() -> None:
    usage = QuotaUsage(capsules=1, total_bytes=500, truncated=True)
    checker = QuotaChecker(
        QuotaConfig(max_bytes_hard=400), usage_provider=lambda: usage, audit_hook=lambda _p: None,
        alert_hook=lambda _v: None,
    )
    assert checker.check().outcome == "reject"


# --------------------------------------------------------------------------- #
# Where and how often it runs
# --------------------------------------------------------------------------- #


def _request(checker: QuotaChecker) -> Any:
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(quota_checker=checker)),
        state=SimpleNamespace(),
    )


def test_dependency_recounts_off_the_event_loop() -> None:
    seen: dict[str, bool] = {}

    def provider() -> QuotaUsage:
        try:
            asyncio.get_running_loop()
            seen["on_loop"] = True
        except RuntimeError:
            seen["on_loop"] = False
        return QuotaUsage(capsules=0, total_bytes=0)

    checker = QuotaChecker(QuotaConfig(max_capsules_hard=10), usage_provider=provider)
    response = SimpleNamespace(headers={})
    asyncio.run(enforce_storage_quota(_request(checker), response))  # type: ignore[arg-type]
    assert seen == {"on_loop": False}


def test_dependency_still_rejects_over_the_hard_limit() -> None:
    checker = QuotaChecker(
        QuotaConfig(max_capsules_hard=1),
        usage_provider=lambda: QuotaUsage(capsules=5, total_bytes=0),
        audit_hook=lambda _p: None,
        alert_hook=lambda _v: None,
    )
    with pytest.raises(QuotaExceededError):
        asyncio.run(
            enforce_storage_quota(_request(checker), SimpleNamespace(headers={}))  # type: ignore[arg-type]
        )


def test_concurrent_checks_use_the_last_value_while_a_recount_runs() -> None:
    now = [0.0]
    release = threading.Event()
    calls = [0]

    def provider() -> QuotaUsage:
        calls[0] += 1
        if calls[0] == 2:  # the second (stale-refresh) walk blocks
            assert release.wait(10)
        return QuotaUsage(capsules=calls[0], total_bytes=0)

    checker = QuotaChecker(
        QuotaConfig(max_capsules_hard=100), usage_provider=provider, clock=lambda: now[0]
    )
    checker.check()  # primes the cache with capsules=1
    now[0] = 10.0  # past the TTL

    t = threading.Thread(target=checker.check)
    t.start()
    deadline = time.monotonic() + 5
    while calls[0] < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert calls[0] == 2, "the refresh walk never started"

    started = time.monotonic()
    checker.check()  # must NOT queue behind the in-flight walk
    assert time.monotonic() - started < 1.0
    assert calls[0] == 2, "a second walk ran concurrently"

    release.set()
    t.join(5)
    assert not t.is_alive()
