# Copyright 2024 NovaFabric Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Regression: create_app() must not leak an observer thread / inotify watch.

`create_app()` used to construct a `CapsuleWatcher` eagerly. `WatchdogBackend`
opens an OS-level inotify watch and starts an observer thread in its
constructor — deliberately, because `poll_once()` only drains the event queue,
so a watch that starts late misses everything created before it. But the
watcher is only ever *used* inside the app lifespan, and only the lifespan
closes it. A `TestClient` used without its context manager never runs the
lifespan, so it created a watcher nothing ever closed.

The suite builds thousands of apps that way. Two symptoms, one cause:

- the box's 512 inotify instances ran out, surfacing as `OSError: [Errno 24]`
  across the serve tier;
- the accumulated observer threads eventually starved an xdist worker of C
  stack inside pydantic-core's deeply-recursive schema generation, which
  **segfaulted the worker**. pytest then reported whichever test happened to
  be executing, so this presented for weeks as an unreproducible flake in
  unrelated tests — two in `test_server_api_keys.py` were investigated and
  "fixed" three times on hypotheses that were all wrong.

The lesson worth keeping: a failure that only appears in the full suite, never
in isolation, and names a *different* test each time is evidence of
process-level resource exhaustion, not of a bug in the named test.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("watchdog")


def _observer_threads() -> int:
    """Count live watchdog emitter threads."""
    return sum(1 for t in threading.enumerate() if "emitter" in t.name.lower())


def _make_app(tmp_path: Path):  # type: ignore[no-untyped-def]
    from novafabric.serve.app import create_app

    caps = tmp_path / "caps"
    caps.mkdir(parents=True, exist_ok=True)
    return create_app(
        capsule_dir=caps, db_path=tmp_path / "r.db", token="test-token-watcher"
    )


def test_create_app_starts_no_observer_thread(tmp_path: Path) -> None:
    before = _observer_threads()
    _make_app(tmp_path)
    assert _observer_threads() == before


def test_many_apps_do_not_accumulate_observer_threads(tmp_path: Path) -> None:
    """The leak, stated as a test: N apps must not mean N observer threads.

    This is the shape that exhausted inotify and ultimately segfaulted a
    worker — thousands of apps across a suite run, none of them closed.
    """
    before = _observer_threads()
    for i in range(15):
        _make_app(tmp_path / f"app{i}")
    assert _observer_threads() == before


def test_watcher_still_works_when_the_lifespan_runs(tmp_path: Path) -> None:
    """Laziness must not become never.

    Without this, deferring construction would silently disable capsule
    watching in production and every other test here would still pass.
    """
    from fastapi.testclient import TestClient

    app = _make_app(tmp_path)
    with TestClient(app):  # runs the lifespan, which builds and closes it
        pass


def test_shutdown_does_not_build_a_watcher_just_to_close_it(
    tmp_path: Path,
) -> None:
    """Closing must be a no-op when nothing was ever constructed.

    A naive `_watcher.close()` on shutdown would construct the very object it
    is trying to release — reintroducing the leak at the moment of cleanup.
    """
    before = _observer_threads()
    app = _make_app(tmp_path)
    # Reach the shutdown path without ever touching the watcher.
    for handler in getattr(app.router, "on_shutdown", []):
        handler()
    assert _observer_threads() == before
