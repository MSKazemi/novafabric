"""Background capsule watcher — populates runs_cache from the filesystem.

Two backends:
- PollingBackend (default, zero extra deps) — incremental directory scan
- WatchdogBackend (optional, ``pip install novafabric[watch]``) — inotify/FSEvents/kqueue

Backend selection: ``NOVA_WATCHER_BACKEND`` env var (``auto`` | ``polling`` | ``watchdog``).
Poll interval: ``NOVA_WATCHER_INTERVAL`` env var (float seconds, default 2.0).
"""
from __future__ import annotations

import logging
import os
import queue
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger(__name__)


# ── helpers ───────────────────────────────────────────────────────────────


def _manifest_to_summary(capsule_path: Path, m: dict[str, Any]) -> dict[str, Any]:
    """Convert a capsule.yaml dict to a runs_cache-compatible summary dict."""
    return {
        "run_id": m.get("run_id", capsule_path.name),
        "status": m.get("status"),
        "created_at": m.get("created_at"),
        "finished_at": m.get("finished_at"),
        "duration_ms": m.get("duration_ms"),
        "exit_code": m.get("exit_code"),
        "model_call_count": m.get("model_call_count", 0),
        "tool_call_count": m.get("tool_call_count", 0),
        "mutating_tool_count": m.get("mutating_tool_count", 0),
        "command": m.get("command", []),
        "novafabric_version": m.get("novafabric_version"),
        "capsule_path": str(capsule_path.resolve()),
    }


# ── backends ──────────────────────────────────────────────────────────────


class _BackendProtocol(Protocol):
    name: str

    def poll_once(self, capsule_dir: Path, conn: sqlite3.Connection) -> int: ...

    def close(self) -> None: ...


class PollingBackend:
    """Incremental directory scan — always available, zero extra deps."""

    name = "polling"

    def poll_once(self, capsule_dir: Path, conn: sqlite3.Connection) -> int:
        from novafabric.registry.runs_cache import build_runs_index  # noqa: PLC0415
        return build_runs_index(capsule_dir, conn, incremental=True)

    def close(self) -> None:
        pass


class WatchdogBackend:
    """inotify/FSEvents/kqueue event-driven backend.

    Raises ``ImportError`` at construction time when ``watchdog`` is not
    installed (``pip install novafabric[watch]``).
    Registers exactly ONE OS-level watch on the parent capsule directory.
    """

    name = "watchdog"

    def __init__(self, capsule_dir: Path) -> None:
        from watchdog.observers import Observer  # noqa: PLC0415

        self._capsule_dir = capsule_dir
        self._queue: queue.Queue[Path] = queue.Queue()
        self._observer = Observer()
        self._scheduled = False
        if capsule_dir.exists():
            self._schedule()

    def _schedule(self) -> None:
        from watchdog.events import (
            FileSystemEventHandler,  # noqa: PLC0415
        )

        queue_ref = self._queue

        class _Handler(FileSystemEventHandler):
            def on_created(self, event: Any) -> None:
                if event.is_directory:
                    queue_ref.put(Path(event.src_path))

        self._observer.schedule(
            _Handler(), str(self._capsule_dir), recursive=False
        )
        if not self._observer.is_alive():
            self._observer.start()
        self._scheduled = True

    def poll_once(self, capsule_dir: Path, conn: sqlite3.Connection) -> int:
        if not self._scheduled and capsule_dir.exists():
            self._schedule()

        import yaml  # noqa: PLC0415

        from novafabric.registry.runs_cache import upsert_run  # noqa: PLC0415
        from novafabric.serve.capsule_loader import load_capsule_manifest  # noqa: PLC0415

        inserted = 0
        while True:
            try:
                p = self._queue.get_nowait()
            except queue.Empty:
                break
            if not (p / "capsule.yaml").exists():
                continue
            try:
                m = load_capsule_manifest(p)
            except (FileNotFoundError, yaml.YAMLError):
                continue
            upsert_run(conn, _manifest_to_summary(p, m))
            inserted += 1
        return inserted

    def close(self) -> None:
        if self._observer.is_alive():
            self._observer.stop()
            self._observer.join()


# ── main class ────────────────────────────────────────────────────────────


class CapsuleWatcher:
    """Manages capsule-directory indexing into runs_cache.

    Used by ``nova serve`` (background daemon) and ``nova ingest-capsule``
    (standalone CLI).
    """

    def __init__(
        self,
        capsule_dir: Path,
        db_path: Path | None = None,
        interval: float = 2.0,
        backend: str = "auto",
    ) -> None:
        self._capsule_dir = capsule_dir
        self._db_path = db_path
        self._interval = float(os.environ.get("NOVA_WATCHER_INTERVAL", interval))

        resolved = os.environ.get("NOVA_WATCHER_BACKEND", backend)
        if resolved == "watchdog":
            self._backend: _BackendProtocol = WatchdogBackend(capsule_dir)
        elif resolved == "polling":
            self._backend = PollingBackend()
        else:  # "auto"
            try:
                self._backend = WatchdogBackend(capsule_dir)
            except ImportError:
                self._backend = PollingBackend()

    # ── internal ──────────────────────────────────────────────────────────

    def _open_conn(self) -> sqlite3.Connection:
        from novafabric.registry.store import get_connection, init_schema  # noqa: PLC0415
        conn = get_connection(self._db_path)
        init_schema(conn)
        return conn

    # ── public API ────────────────────────────────────────────────────────

    def poll_once(self) -> int:
        """Incremental scan; returns number of new rows inserted."""
        conn = self._open_conn()
        try:
            return self._backend.poll_once(self._capsule_dir, conn)
        finally:
            conn.close()

    def ingest_one(self, run_id_or_path: str | Path) -> tuple[bool, bool]:
        """Ingest a single capsule.

        Returns ``(found, is_new)``:
        - ``(False, False)`` — capsule directory or capsule.yaml not found
        - ``(True, True)``  — found and newly inserted
        - ``(True, False)`` — found and existing row updated
        """
        import yaml  # noqa: PLC0415

        from novafabric.registry.runs_cache import upsert_run  # noqa: PLC0415
        from novafabric.serve.capsule_loader import load_capsule_manifest  # noqa: PLC0415

        p = Path(run_id_or_path)
        if not p.is_absolute():
            p = self._capsule_dir / str(run_id_or_path)
        if not p.exists() or not (p / "capsule.yaml").exists():
            return False, False
        try:
            m = load_capsule_manifest(p)
        except (FileNotFoundError, yaml.YAMLError):
            return False, False

        conn = self._open_conn()
        try:
            run_id = m.get("run_id", p.name)
            is_new = not bool(
                conn.execute(
                    "SELECT 1 FROM runs_cache WHERE run_id = ?", (run_id,)
                ).fetchone()
            )
            upsert_run(conn, _manifest_to_summary(p, m))
            conn.commit()
        finally:
            conn.close()
        return True, is_new

    def ingest_all(self) -> int:
        """Full non-incremental re-index; returns rows inserted/replaced."""
        from novafabric.registry.runs_cache import build_runs_index  # noqa: PLC0415
        conn = self._open_conn()
        try:
            return build_runs_index(self._capsule_dir, conn, incremental=False)
        finally:
            conn.close()

    def run_forever(self) -> None:
        """Polling loop; exits cleanly on KeyboardInterrupt."""
        try:
            while True:
                try:
                    self.poll_once()
                except Exception as _exc:  # noqa: BLE001
                    logger.debug("capsule watcher: poll error: %s", _exc)
                time.sleep(self._interval)
        except KeyboardInterrupt:
            pass

    def start_background(self) -> threading.Thread:
        """Start ``run_forever()`` in a daemon thread and return it."""
        t = threading.Thread(
            target=self.run_forever, daemon=True, name="nova-capsule-watcher"
        )
        t.start()
        return t

    def backend_name(self) -> str:
        """Return ``'polling'`` or ``'watchdog'``."""
        return self._backend.name

    def close(self) -> None:
        """Stop any OS-level watchers (WatchdogBackend only)."""
        self._backend.close()
