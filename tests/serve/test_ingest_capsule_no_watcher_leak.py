"""``POST /api/ingest-capsule`` must not leak a file watcher per request (review r6).

The route built a fresh ``CapsuleWatcher`` on every request and never closed it. With
the watchdog backend (the ``auto`` default when ``watchdog`` is installed) that is one
inotify instance plus an observer thread and an emitter thread per request, kept
until the process exits. Linux's default ``fs.inotify.max_user_instances`` is 128, so
about 128 authenticated requests exhausted inotify for the whole user, the server's
own background watcher included.

The route only indexes (``ingest_all`` / ``ingest_one``), which never reads the
watch backend, so it now uses an indexer with no OS watch at all. The test sends N
requests with the watchdog backend forced and requires the inotify-descriptor and
thread counts to stay where they were.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("watchdog")

from fastapi.testclient import TestClient  # noqa: E402

from novafabric.serve.app import create_app  # noqa: E402

TOKEN = "t" * 32
N_REQUESTS = 12


def _inotify_fds() -> int:
    count = 0
    for fd in os.listdir("/proc/self/fd"):
        try:
            if "inotify" in os.readlink(f"/proc/self/fd/{fd}"):
                count += 1
        except OSError:
            continue  # closed between listdir and readlink
    return count


def _watch_threads() -> int:
    """Live watchdog threads (observer, emitter, inotify buffer).

    Counted by the thread's class module: watchdog names its threads ``Thread-N``,
    so a name match would count nothing and pass vacuously.
    """
    return sum(
        1 for t in threading.enumerate() if type(t).__module__.startswith("watchdog.")
    )


@pytest.fixture()
def capsule_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    if not Path("/proc/self/fd").is_dir():
        pytest.skip("needs /proc to count inotify descriptors")
    monkeypatch.setenv("NOVA_WATCHER_BACKEND", "watchdog")
    caps = tmp_path / "capsules"
    caps.mkdir()
    return caps


def test_repeated_ingest_all_keeps_watchers_and_threads_bounded(
    capsule_dir: Path, tmp_path: Path
) -> None:
    app = create_app(token=TOKEN, capsule_dir=capsule_dir, db_path=tmp_path / "r.db")
    client = TestClient(app, base_url="http://127.0.0.1")  # no lifespan: nothing would close
    fds_before, threads_before = _inotify_fds(), _watch_threads()

    for _ in range(N_REQUESTS):
        res = client.post(
            "/api/ingest-capsule",
            json={"all": True},
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        assert res.status_code == 200, res.text

    assert _watch_threads() == threads_before, "each request left watcher threads running"
    assert _inotify_fds() == fds_before, "each request left an inotify instance open"


def test_repeated_ingest_one_keeps_watchers_and_threads_bounded(
    capsule_dir: Path, tmp_path: Path
) -> None:
    app = create_app(token=TOKEN, capsule_dir=capsule_dir, db_path=tmp_path / "r.db")
    client = TestClient(app, base_url="http://127.0.0.1")
    fds_before, threads_before = _inotify_fds(), _watch_threads()

    for i in range(N_REQUESTS):
        res = client.post(
            "/api/ingest-capsule",
            json={"run_id": f"missing-{i}"},
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        assert res.status_code == 404, res.text

    assert _inotify_fds() == fds_before
    assert _watch_threads() == threads_before


def test_ingest_one_still_indexes_a_capsule(capsule_dir: Path, tmp_path: Path) -> None:
    run = capsule_dir / "run-r6"
    run.mkdir()
    (run / "capsule.yaml").write_text("run_id: run-r6\nstatus: completed\n")
    app = create_app(token=TOKEN, capsule_dir=capsule_dir, db_path=tmp_path / "r.db")
    client = TestClient(app, base_url="http://127.0.0.1")

    res = client.post(
        "/api/ingest-capsule",
        json={"run_id": "run-r6"},
        headers={"Authorization": f"Bearer {TOKEN}"},
    )

    assert res.status_code == 200, res.text
    assert res.json() == {"ok": True, "found": True, "is_new": True}
