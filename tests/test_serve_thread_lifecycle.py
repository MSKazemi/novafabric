"""Regression tests for the serve background-thread lifecycle (suite-health).

The stats-refresh / SSE-publish / incremental-index daemon thread (and the
CapsuleWatcher) must be tied to the app *lifespan*, not to ``create_app()``.
Otherwise every ``create_app()`` in the test suite leaks one un-joined daemon
thread, and the suite crawls (observed: ~2,319 live threads at ~89 % of a run).
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

from fastapi.testclient import TestClient

from novafabric.serve.app import create_app

TOKEN = "test-token"  # noqa: S105


def _stats_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == "nova-serve-stats-refresh"]


def test_create_app_does_not_start_thread_without_lifespan(tmp_path: Path) -> None:
    """Constructing the app (no lifespan) must not spawn the refresh thread."""
    before = len(_stats_threads())
    app = create_app(token=TOKEN, capsule_dir=tmp_path, db_path=None)
    assert app is not None
    # No TestClient context entered → lifespan never runs → no thread.
    assert len(_stats_threads()) == before


def test_lifespan_starts_and_stops_thread(tmp_path: Path) -> None:
    """Entering the TestClient context starts the thread; exit joins it."""
    before = len(_stats_threads())
    app = create_app(token=TOKEN, capsule_dir=tmp_path, db_path=None)
    with TestClient(app):
        # Lifespan has run → exactly one refresh thread is live.
        assert len(_stats_threads()) == before + 1
    # Context exited → lifespan finally set the stop event and joined the thread.
    # Allow a brief moment for the join to settle on slow CI.
    for _ in range(50):
        if len(_stats_threads()) == before:
            break
        time.sleep(0.05)
    assert len(_stats_threads()) == before


def test_repeated_apps_do_not_accumulate_threads(tmp_path: Path) -> None:
    """The leak regression: N lifespan cycles must return to baseline, not N threads."""
    before = len(_stats_threads())
    for i in range(5):
        app = create_app(token=TOKEN, capsule_dir=tmp_path / f"a{i}", db_path=None)
        with TestClient(app):
            pass
    for _ in range(50):
        if len(_stats_threads()) == before:
            break
        time.sleep(0.05)
    assert len(_stats_threads()) == before
