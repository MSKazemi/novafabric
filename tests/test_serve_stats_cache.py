"""Unit tests for the thread-safe ``_StatsCache`` (D1d).

These verify that concurrent cache-miss requests do not all recompute the
snapshot — only the first thread through the lock runs ``compute``.
"""
from __future__ import annotations

import threading
import time

import pytest

pytest.importorskip("fastapi")


def _new_cache():
    from novafabric.serve.app import _StatsCache

    return _StatsCache()


def test_get_or_compute_returns_value() -> None:
    cache = _new_cache()
    out = cache.get_or_compute(lambda: {"runs": 7})
    assert out == {"runs": 7}


def test_get_or_compute_warms_cache() -> None:
    cache = _new_cache()
    cache.get_or_compute(lambda: {"runs": 1})
    # second call must hit the warm cache and not invoke compute again
    out = cache.get_or_compute(lambda: pytest.fail("should not recompute"))  # type: ignore[arg-type]
    assert out == {"runs": 1}


def test_concurrent_cold_start_computes_once() -> None:
    cache = _new_cache()
    calls = {"n": 0}
    call_lock = threading.Lock()

    def compute() -> dict[str, int]:
        with call_lock:
            calls["n"] += 1
        time.sleep(0.05)  # widen the race window
        return {"runs": 42}

    results: list[dict[str, int]] = []
    res_lock = threading.Lock()

    def worker() -> None:
        r = cache.get_or_compute(compute)
        with res_lock:
            results.append(r)

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Double-checked locking: only one thread should have run compute.
    assert calls["n"] == 1
    assert len(results) == 16
    assert all(r == {"runs": 42} for r in results)


def test_get_is_thread_safe_under_concurrent_set() -> None:
    """get()/set() must not raise under concurrent access."""
    cache = _new_cache()
    stop = threading.Event()
    errors: list[Exception] = []

    def setter() -> None:
        i = 0
        while not stop.is_set():
            try:
                cache.set({"runs": i})
                i += 1
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

    def getter() -> None:
        while not stop.is_set():
            try:
                cache.get()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

    threads = [threading.Thread(target=setter) for _ in range(4)]
    threads += [threading.Thread(target=getter) for _ in range(4)]
    for t in threads:
        t.start()
    time.sleep(0.2)
    stop.set()
    for t in threads:
        t.join()

    assert not errors
