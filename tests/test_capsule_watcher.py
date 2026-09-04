"""Tests for Scale-S3: CapsuleWatcher + backends."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
import yaml


def _make_capsule(base: Path, run_id: str, status: str = "success") -> Path:
    d = base / run_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "capsule.yaml").write_text(
        yaml.dump({
            "run_id": run_id,
            "status": status,
            "created_at": "2026-01-01T10:00:00Z",
            "finished_at": "2026-01-01T10:00:01Z",
            "duration_ms": 1000,
            "exit_code": 0,
            "model_call_count": 1,
            "tool_call_count": 0,
            "mutating_tool_count": 0,
            "command": ["python", "agent.py"],
            "novafabric_version": "0.35.0",
        })
    )
    return d


def _open_mem_conn() -> sqlite3.Connection:
    """In-memory SQLite with Row factory."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn


# ── PollingBackend ─────────────────────────────────────────────────────────

class TestPollingBackend:
    def test_poll_once_finds_new_capsule(self, tmp_path: Path) -> None:
        from novafabric.registry.runs_cache import count_cached_runs, ensure_runs_cache
        from novafabric.serve.capsule_watcher import PollingBackend

        _make_capsule(tmp_path, "run_001")
        conn = _open_mem_conn()
        ensure_runs_cache(conn)

        backend = PollingBackend()
        n = backend.poll_once(tmp_path, conn)
        conn.commit()

        assert n == 1
        assert count_cached_runs(conn) == 1

    def test_poll_once_incremental_skips_existing(self, tmp_path: Path) -> None:
        from novafabric.registry.runs_cache import ensure_runs_cache
        from novafabric.serve.capsule_watcher import PollingBackend

        _make_capsule(tmp_path, "run_001")
        conn = _open_mem_conn()
        ensure_runs_cache(conn)
        backend = PollingBackend()
        backend.poll_once(tmp_path, conn)
        conn.commit()

        n = backend.poll_once(tmp_path, conn)
        conn.commit()
        assert n == 0

    def test_poll_once_skips_dir_without_yaml(self, tmp_path: Path) -> None:
        from novafabric.registry.runs_cache import count_cached_runs, ensure_runs_cache
        from novafabric.serve.capsule_watcher import PollingBackend

        (tmp_path / "not-a-capsule").mkdir()
        conn = _open_mem_conn()
        ensure_runs_cache(conn)
        backend = PollingBackend()
        backend.poll_once(tmp_path, conn)
        conn.commit()
        assert count_cached_runs(conn) == 0

    def test_close_is_noop(self) -> None:
        from novafabric.serve.capsule_watcher import PollingBackend
        PollingBackend().close()  # must not raise


# ── CapsuleWatcher core ────────────────────────────────────────────────────

class TestCapsuleWatcherCore:
    def test_poll_once_returns_new_row_count(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from novafabric.serve.capsule_watcher import CapsuleWatcher

        db = tmp_path / "registry.db"
        monkeypatch.setenv("NOVA_WATCHER_BACKEND", "polling")
        _make_capsule(tmp_path / "caps", "run_a")
        _make_capsule(tmp_path / "caps", "run_b")

        watcher = CapsuleWatcher(tmp_path / "caps", db_path=db, backend="polling")
        n = watcher.poll_once()
        assert n == 2

    def test_poll_once_incremental_on_second_call(self, tmp_path: Path) -> None:
        from novafabric.serve.capsule_watcher import CapsuleWatcher

        caps = tmp_path / "caps"
        caps.mkdir()
        db = tmp_path / "registry.db"
        _make_capsule(caps, "run_001")
        watcher = CapsuleWatcher(caps, db_path=db, backend="polling")
        watcher.poll_once()

        _make_capsule(caps, "run_002")
        n = watcher.poll_once()
        assert n == 1

    def test_ingest_all_full_rescan(self, tmp_path: Path) -> None:
        from novafabric.serve.capsule_watcher import CapsuleWatcher

        caps = tmp_path / "caps"
        caps.mkdir()
        db = tmp_path / "registry.db"
        for i in range(5):
            _make_capsule(caps, f"run_{i:03d}")
        watcher = CapsuleWatcher(caps, db_path=db, backend="polling")
        n = watcher.ingest_all()
        assert n == 5

    def test_ingest_all_replaces_stale_rows(self, tmp_path: Path) -> None:
        from novafabric.registry.runs_cache import query_runs
        from novafabric.registry.store import get_connection
        from novafabric.serve.capsule_watcher import CapsuleWatcher

        caps = tmp_path / "caps"
        caps.mkdir()
        db = tmp_path / "registry.db"
        _make_capsule(caps, "run_001", status="running")
        watcher = CapsuleWatcher(caps, db_path=db, backend="polling")
        watcher.ingest_all()

        # Update capsule.yaml on disk
        (caps / "run_001" / "capsule.yaml").write_text(
            yaml.dump({"run_id": "run_001", "status": "success", "created_at": "2026-01-01T10:00:00Z"})
        )
        watcher.ingest_all()

        conn = get_connection(db)
        rows, _ = query_runs(conn)
        conn.close()
        assert rows[0]["status"] == "success"

    def test_backend_name_polling(self, tmp_path: Path) -> None:
        from novafabric.serve.capsule_watcher import CapsuleWatcher

        w = CapsuleWatcher(tmp_path, db_path=tmp_path / "r.db", backend="polling")
        assert w.backend_name() == "polling"

    def test_close_noop_for_polling(self, tmp_path: Path) -> None:
        from novafabric.serve.capsule_watcher import CapsuleWatcher

        w = CapsuleWatcher(tmp_path, db_path=tmp_path / "r.db", backend="polling")
        w.close()  # must not raise


# ── CapsuleWatcher.ingest_one ─────────────────────────────────────────────

class TestCapsuleWatcherIngestOne:
    def test_ingest_one_by_run_id(self, tmp_path: Path) -> None:
        from novafabric.serve.capsule_watcher import CapsuleWatcher

        caps = tmp_path / "caps"
        caps.mkdir()
        _make_capsule(caps, "run_abc")
        db = tmp_path / "registry.db"
        watcher = CapsuleWatcher(caps, db_path=db, backend="polling")

        found, is_new = watcher.ingest_one("run_abc")
        assert found is True
        assert is_new is True

    def test_ingest_one_by_absolute_path(self, tmp_path: Path) -> None:
        from novafabric.serve.capsule_watcher import CapsuleWatcher

        caps = tmp_path / "caps"
        caps.mkdir()
        d = _make_capsule(caps, "run_xyz")
        db = tmp_path / "registry.db"
        watcher = CapsuleWatcher(caps, db_path=db, backend="polling")

        found, is_new = watcher.ingest_one(d)
        assert found is True
        assert is_new is True

    def test_ingest_one_not_found_returns_false(self, tmp_path: Path) -> None:
        from novafabric.serve.capsule_watcher import CapsuleWatcher

        caps = tmp_path / "caps"
        caps.mkdir()
        db = tmp_path / "registry.db"
        watcher = CapsuleWatcher(caps, db_path=db, backend="polling")

        found, is_new = watcher.ingest_one("nonexistent-run")
        assert found is False
        assert is_new is False

    def test_ingest_one_updated_when_already_cached(self, tmp_path: Path) -> None:
        from novafabric.serve.capsule_watcher import CapsuleWatcher

        caps = tmp_path / "caps"
        caps.mkdir()
        _make_capsule(caps, "run_001")
        db = tmp_path / "registry.db"
        watcher = CapsuleWatcher(caps, db_path=db, backend="polling")

        _, first_new = watcher.ingest_one("run_001")
        _, second_new = watcher.ingest_one("run_001")

        assert first_new is True
        assert second_new is False   # was already in cache

    def test_ingest_one_always_upserts_stale_data(self, tmp_path: Path) -> None:
        from novafabric.registry.runs_cache import query_runs
        from novafabric.registry.store import get_connection
        from novafabric.serve.capsule_watcher import CapsuleWatcher

        caps = tmp_path / "caps"
        caps.mkdir()
        _make_capsule(caps, "run_001", status="running")
        db = tmp_path / "registry.db"
        watcher = CapsuleWatcher(caps, db_path=db, backend="polling")
        watcher.ingest_one("run_001")

        # Update on disk
        (caps / "run_001" / "capsule.yaml").write_text(
            yaml.dump({"run_id": "run_001", "status": "success", "created_at": "2026-01-01T10:00:00Z"})
        )
        watcher.ingest_one("run_001")  # must refresh — not skip

        conn = get_connection(db)
        rows, _ = query_runs(conn)
        conn.close()
        assert rows[0]["status"] == "success"


# ── CapsuleWatcher.start_background + run_forever ──────────────────────────

class TestCapsuleWatcherBackground:
    def test_start_background_returns_alive_thread(self, tmp_path: Path) -> None:
        from novafabric.serve.capsule_watcher import CapsuleWatcher

        caps = tmp_path / "caps"
        caps.mkdir()
        db = tmp_path / "registry.db"
        watcher = CapsuleWatcher(caps, db_path=db, backend="polling", interval=60.0)
        t = watcher.start_background()

        assert t.is_alive()
        assert t.daemon is True
        t.join(timeout=0)  # non-blocking check — thread is still running
        watcher.close()
        t.join(timeout=5.0)

    def test_run_forever_indexes_new_capsule_within_3s(self, tmp_path: Path) -> None:
        """5-second SLA: capsule created after watcher starts must be indexed."""
        import time

        from novafabric.registry.runs_cache import count_cached_runs
        from novafabric.registry.store import get_connection, init_schema
        from novafabric.serve.capsule_watcher import CapsuleWatcher

        caps = tmp_path / "caps"
        caps.mkdir()
        db = tmp_path / "registry.db"

        # Initialize schema before starting watcher
        conn = get_connection(db)
        init_schema(conn)
        conn.close()

        watcher = CapsuleWatcher(caps, db_path=db, backend="polling", interval=0.5)
        t = watcher.start_background()

        # Create capsule AFTER watcher starts
        _make_capsule(caps, "run_late")

        # Allow up to 3 s — well within the 5 s SLA (0.5 s interval)
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            conn = get_connection(db)
            n = count_cached_runs(conn)
            conn.close()
            if n > 0:
                break
            time.sleep(0.1)

        conn = get_connection(db)
        n = count_cached_runs(conn)
        conn.close()
        assert n == 1, "Capsule was not indexed within 3 s (SLA: 5 s)"
        watcher.close()
        t.join(timeout=5.0)


# ── WatchdogBackend ────────────────────────────────────────────────────────

def _has_watchdog() -> bool:
    """Check if watchdog module is installed."""
    try:
        import watchdog  # noqa: F401, PLC0415
        return True
    except ImportError:
        return False


class TestWatchdogBackend:
    def test_backend_name_is_watchdog(self, tmp_path: Path) -> None:
        if not _has_watchdog():
            pytest.skip("watchdog not installed — skip WatchdogBackend tests")
        from novafabric.serve.capsule_watcher import WatchdogBackend

        caps = tmp_path / "caps"
        caps.mkdir()
        backend = WatchdogBackend(caps)
        assert backend.name == "watchdog"
        backend.close()

    def test_detects_new_capsule_dir(self, tmp_path: Path) -> None:
        """Observer fires an event and poll_once picks it up."""
        if not _has_watchdog():
            pytest.skip("watchdog not installed — skip WatchdogBackend tests")
        import time

        from novafabric.registry.runs_cache import count_cached_runs, ensure_runs_cache
        from novafabric.serve.capsule_watcher import WatchdogBackend

        caps = tmp_path / "caps"
        caps.mkdir()
        conn = sqlite3.connect(str(tmp_path / "r.db"))
        conn.row_factory = sqlite3.Row
        ensure_runs_cache(conn)

        backend = WatchdogBackend(caps)
        _make_capsule(caps, "run_watch_001")

        # Give inotify a moment to fire
        time.sleep(0.3)

        n = backend.poll_once(caps, conn)
        conn.commit()
        backend.close()

        assert n == 1
        assert count_cached_runs(conn) == 1

    def test_close_stops_observer(self, tmp_path: Path) -> None:
        if not _has_watchdog():
            pytest.skip("watchdog not installed — skip WatchdogBackend tests")
        from novafabric.serve.capsule_watcher import WatchdogBackend

        caps = tmp_path / "caps"
        caps.mkdir()
        backend = WatchdogBackend(caps)
        assert backend._observer.is_alive()
        backend.close()
        assert not backend._observer.is_alive()

    def test_capsule_watcher_auto_selects_watchdog(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        if not _has_watchdog():
            pytest.skip("watchdog not installed — skip WatchdogBackend tests")
        from novafabric.serve.capsule_watcher import CapsuleWatcher, WatchdogBackend

        monkeypatch.delenv("NOVA_WATCHER_BACKEND", raising=False)
        caps = tmp_path / "caps"
        caps.mkdir()
        w = CapsuleWatcher(caps, db_path=tmp_path / "r.db", backend="auto")
        assert isinstance(w._backend, WatchdogBackend)
        w.close()


# ── CLI tests ──────────────────────────────────────────────────────────────


class TestIngestCapsuleCLI:
    def test_single_ingest_success(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from typer.testing import CliRunner

        from novafabric.cli.ingest_capsule import app as ingest_app

        caps = tmp_path / "caps"
        caps.mkdir()
        _make_capsule(caps, "run_cli_001")
        db = tmp_path / "registry.db"
        runner = CliRunner()

        result = runner.invoke(ingest_app, [
            "run_cli_001",
            "--capsule-dir", str(caps),
            "--db-path", str(db),
        ])
        assert result.exit_code == 0
        assert "indexed" in result.output
        assert "run_cli_001" in result.output
        assert "[new]" in result.output

    def test_single_ingest_updated_label(self, tmp_path: Path) -> None:
        from typer.testing import CliRunner

        from novafabric.cli.ingest_capsule import app as ingest_app

        caps = tmp_path / "caps"
        caps.mkdir()
        _make_capsule(caps, "run_cli_002")
        db = tmp_path / "registry.db"
        runner = CliRunner()
        args = ["run_cli_002", "--capsule-dir", str(caps), "--db-path", str(db)]

        runner.invoke(ingest_app, args)
        result = runner.invoke(ingest_app, args)
        assert result.exit_code == 0
        assert "[updated]" in result.output

    def test_single_ingest_not_found_exits_1(self, tmp_path: Path) -> None:
        from typer.testing import CliRunner

        from novafabric.cli.ingest_capsule import app as ingest_app

        caps = tmp_path / "caps"
        caps.mkdir()
        db = tmp_path / "registry.db"
        runner = CliRunner()

        result = runner.invoke(ingest_app, [
            "ghost-run-id",
            "--capsule-dir", str(caps),
            "--db-path", str(db),
        ])
        assert result.exit_code == 1
        assert "not found" in result.output

    def test_all_flag_indexes_multiple(self, tmp_path: Path) -> None:
        from typer.testing import CliRunner

        from novafabric.cli.ingest_capsule import app as ingest_app

        caps = tmp_path / "caps"
        caps.mkdir()
        for i in range(3):
            _make_capsule(caps, f"run_batch_{i}")
        db = tmp_path / "registry.db"
        runner = CliRunner()

        result = runner.invoke(ingest_app, [
            "--all",
            "--capsule-dir", str(caps),
            "--db-path", str(db),
        ])
        assert result.exit_code == 0
        assert "3" in result.output

    def test_no_args_exits_1(self, tmp_path: Path) -> None:
        from typer.testing import CliRunner

        from novafabric.cli.ingest_capsule import app as ingest_app

        runner = CliRunner()
        result = runner.invoke(ingest_app, [
            "--capsule-dir", str(tmp_path),
            "--db-path", str(tmp_path / "r.db"),
        ])
        assert result.exit_code == 1

    def test_watch_flag_calls_run_forever(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from unittest.mock import patch

        from typer.testing import CliRunner

        from novafabric.cli.ingest_capsule import app as ingest_app

        caps = tmp_path / "caps"
        caps.mkdir()
        db = tmp_path / "registry.db"
        runner = CliRunner()

        # Patch run_forever to raise KeyboardInterrupt immediately
        with patch("novafabric.serve.capsule_watcher.CapsuleWatcher.poll_once", return_value=0):
            with patch("time.sleep", side_effect=KeyboardInterrupt):
                result = runner.invoke(ingest_app, [
                    "--watch",
                    "--capsule-dir", str(caps),
                    "--db-path", str(db),
                ])
        assert result.exit_code == 0
        assert "watching" in result.output
        assert "stopped" in result.output

    def test_watch_prints_new_capsule(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        from typer.testing import CliRunner

        from novafabric.cli.ingest_capsule import app as ingest_app

        caps = tmp_path / "caps"
        caps.mkdir()
        db = tmp_path / "registry.db"
        _make_capsule(caps, "run_watch_new")
        runner = CliRunner()

        call_count = 0

        def _poll_once_then_stop() -> int:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return 1  # signal a new capsule on first poll
            raise KeyboardInterrupt

        with patch(
            "novafabric.serve.capsule_watcher.CapsuleWatcher.poll_once",
            side_effect=_poll_once_then_stop,
        ):
            with patch("time.sleep"):
                result = runner.invoke(ingest_app, [
                    "--watch",
                    "--capsule-dir", str(caps),
                    "--db-path", str(db),
                    "--interval", "0.1",
                ])
        assert result.exit_code == 0
        assert "watching" in result.output
        assert "stopped" in result.output

    def test_watch_capsule_dir_missing_continues(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        from typer.testing import CliRunner

        from novafabric.cli.ingest_capsule import app as ingest_app

        missing = tmp_path / "does_not_exist"
        db = tmp_path / "registry.db"
        runner = CliRunner()

        with patch("novafabric.serve.capsule_watcher.CapsuleWatcher.poll_once", return_value=0):
            with patch("time.sleep", side_effect=KeyboardInterrupt):
                result = runner.invoke(ingest_app, [
                    "--watch",
                    "--capsule-dir", str(missing),
                    "--db-path", str(db),
                ])
        assert result.exit_code == 0
        assert "warning" in result.output.lower()
        assert "stopped" in result.output


# ── WatchdogBackend bounded queue (no-unbounded-queues rule) ──────────────


class TestWatchdogBackendBoundedQueue:
    """The event queue is bounded, and overflow must DEGRADE, never DROP.

    A dropped create event here is a capsule that silently never reaches the
    index while everything reports healthy — the misreport failure class.
    So the contract is: queue full → flag → next poll_once runs the polling
    backend's incremental rescan, and every capsule still gets indexed.
    """

    def test_queue_is_bounded(self, tmp_path: Path) -> None:
        if not _has_watchdog():
            pytest.skip("watchdog not installed — skip WatchdogBackend tests")
        from novafabric.serve.capsule_watcher import WatchdogBackend

        caps = tmp_path / "caps"
        caps.mkdir()
        backend = WatchdogBackend(caps)
        try:
            assert backend._queue.maxsize == WatchdogBackend.QUEUE_MAX > 0
        finally:
            backend.close()

    def test_overflow_degrades_to_rescan_and_loses_nothing(self, tmp_path: Path) -> None:
        """More create events than the queue holds -> every capsule indexed."""
        if not _has_watchdog():
            pytest.skip("watchdog not installed — skip WatchdogBackend tests")
        import queue as queue_mod

        from novafabric.registry.runs_cache import count_cached_runs, ensure_runs_cache
        from novafabric.serve.capsule_watcher import WatchdogBackend

        caps = tmp_path / "caps"
        caps.mkdir()
        conn = sqlite3.connect(str(tmp_path / "r.db"))
        conn.row_factory = sqlite3.Row
        ensure_runs_cache(conn)

        backend = WatchdogBackend(caps)
        try:
            # Shrink the bound so the test does not need 4096 files, then
            # simulate the observer thread outrunning the drain: more real
            # capsules than the queue can hold, enqueued the way the handler
            # does it (put_nowait; on Full set the overflow flag).
            backend._queue = queue_mod.Queue(maxsize=3)
            total = 8
            for i in range(total):
                path = _make_capsule(caps, f"run_burst_{i:03d}")
                try:
                    backend._queue.put_nowait(path)
                except queue_mod.Full:
                    backend._overflowed.set()

            assert backend._overflowed.is_set(), (
                "8 events into a maxsize-3 queue must overflow — if this "
                "fails the test itself is vacuous"
            )

            n = backend.poll_once(caps, conn)
            conn.commit()

            assert count_cached_runs(conn) == total, (
                "overflow dropped capsules from the index instead of "
                "degrading to a rescan"
            )
            assert n >= total
            assert not backend._overflowed.is_set(), "flag must clear after the rescan"
        finally:
            backend.close()

    def test_no_overflow_means_no_rescan_flag(self, tmp_path: Path) -> None:
        if not _has_watchdog():
            pytest.skip("watchdog not installed — skip WatchdogBackend tests")
        from novafabric.registry.runs_cache import ensure_runs_cache
        from novafabric.serve.capsule_watcher import WatchdogBackend

        caps = tmp_path / "caps"
        caps.mkdir()
        conn = sqlite3.connect(str(tmp_path / "r.db"))
        conn.row_factory = sqlite3.Row
        ensure_runs_cache(conn)

        backend = WatchdogBackend(caps)
        try:
            _make_capsule(caps, "run_single")
            import time

            time.sleep(0.3)  # let inotify fire
            backend.poll_once(caps, conn)
            assert not backend._overflowed.is_set()
        finally:
            backend.close()
