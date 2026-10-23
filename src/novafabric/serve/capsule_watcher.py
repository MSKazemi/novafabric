"""Background capsule watcher — populates runs_cache from the filesystem.

Two backends:
- PollingBackend (default, zero extra deps) — incremental directory scan
- WatchdogBackend (optional, ``pip install novafabric[watch]``) — inotify/FSEvents/kqueue

Settings resolve as **explicit argument (CLI flag) > environment > default**
(:func:`resolve_watcher_settings`):

- backend: ``--backend`` > ``NOVA_WATCHER_BACKEND`` > ``auto``
  (``auto`` | ``polling`` | ``watchdog``);
- poll interval: ``--interval`` > ``NOVA_WATCHER_INTERVAL`` > ``2.0`` seconds.

An unknown backend or a non-positive / non-finite interval raises
:class:`WatcherConfigError` naming where the value came from. Until audit
L27/L30 an unknown backend silently became ``auto`` and the environment
overrode an explicit flag.
"""
from __future__ import annotations

import logging
import math
import os
import queue
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from novafabric._capsule_fs import is_capsule_regular_file

logger = logging.getLogger(__name__)

BACKEND_ENV = "NOVA_WATCHER_BACKEND"
INTERVAL_ENV = "NOVA_WATCHER_INTERVAL"
VALID_BACKENDS: tuple[str, ...] = ("auto", "polling", "watchdog")
DEFAULT_BACKEND = "auto"
DEFAULT_INTERVAL = 2.0

SettingSource = Literal["flag", "env", "default"]


class WatcherConfigError(ValueError):
    """A capsule-watcher backend or interval setting is not usable."""


@dataclass(frozen=True)
class WatcherSettings:
    """The effective watcher settings and where each one came from."""

    backend: str
    interval: float
    backend_source: SettingSource
    interval_source: SettingSource


def _where(source: SettingSource, flag: str, env: str) -> str:
    return flag if source == "flag" else env


def _check_backend(raw: str, source: SettingSource) -> str:
    value = raw.strip().lower()
    if value not in VALID_BACKENDS:
        raise WatcherConfigError(
            f"unknown capsule-watcher backend {raw!r} from "
            f"{_where(source, '--backend', BACKEND_ENV)}; "
            f"choose one of: {', '.join(VALID_BACKENDS)}"
        )
    return value


def _check_interval(raw: str | float, source: SettingSource) -> float:
    where = _where(source, "--interval", INTERVAL_ENV)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise WatcherConfigError(
            f"capsule-watcher interval {raw!r} from {where} is not a number of seconds"
        ) from None
    if not math.isfinite(value) or value <= 0:
        raise WatcherConfigError(
            f"capsule-watcher interval {raw!r} from {where} must be a finite number > 0"
        )
    return value


def resolve_watcher_settings(
    backend: str | None = None, interval: float | None = None
) -> WatcherSettings:
    """Resolve backend + interval: explicit argument > environment > default.

    ``None`` means "not given explicitly". Raises :class:`WatcherConfigError`
    for an unknown backend or an unusable interval, whatever its source — a
    typo is refused rather than silently replaced with ``auto``.
    """
    b_source: SettingSource
    if backend is not None:
        b_raw, b_source = backend, "flag"
    elif BACKEND_ENV in os.environ:
        b_raw, b_source = os.environ[BACKEND_ENV], "env"
    else:
        b_raw, b_source = DEFAULT_BACKEND, "default"

    i_source: SettingSource
    i_raw: str | float
    if interval is not None:
        i_raw, i_source = interval, "flag"
    elif INTERVAL_ENV in os.environ:
        i_raw, i_source = os.environ[INTERVAL_ENV], "env"
    else:
        i_raw, i_source = DEFAULT_INTERVAL, "default"

    return WatcherSettings(
        backend=_check_backend(b_raw, b_source),
        interval=_check_interval(i_raw, i_source),
        backend_source=b_source,
        interval_source=i_source,
    )


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

    # Bound on the event queue (project rule: no unbounded queues — the
    # observer thread produces, poll_once consumes every NOVA_WATCHER_INTERVAL
    # seconds, and a mass ingest can create directories faster than the drain).
    # 4096 Path entries is far past any burst a 2 s poll window has seen, and
    # overflow DEGRADES to a full incremental rescan rather than dropping —
    # a dropped create event here would mean a capsule that silently never
    # reaches the index, which is exactly the misreport class this system
    # exists to prevent.
    QUEUE_MAX = 4096

    def __init__(self, capsule_dir: Path) -> None:
        from watchdog.observers import Observer  # noqa: PLC0415

        self._capsule_dir = capsule_dir
        self._queue: queue.Queue[Path] = queue.Queue(maxsize=self.QUEUE_MAX)
        self._overflowed = threading.Event()
        self._observer = Observer()
        self._scheduled = False
        if capsule_dir.exists():
            self._schedule()

    def _schedule(self) -> None:
        from watchdog.events import (
            FileSystemEventHandler,  # noqa: PLC0415
        )

        queue_ref = self._queue
        overflow_ref = self._overflowed

        class _Handler(FileSystemEventHandler):
            def on_created(self, event: Any) -> None:
                if not event.is_directory:
                    return
                try:
                    queue_ref.put_nowait(Path(event.src_path))
                except queue.Full:
                    # Never block or raise in the observer thread. The flag
                    # makes the next poll_once run a full incremental rescan,
                    # which indexes this capsule anyway — degraded, not lost.
                    overflow_ref.set()

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
        if self._overflowed.is_set():
            # The bounded queue overflowed since the last poll: events were
            # not enqueued, so the queue alone is no longer a complete record
            # of what was created. Fall back to the polling backend's
            # incremental rescan (idempotent upserts make any double-visit of
            # still-queued paths harmless), then continue draining normally.
            self._overflowed.clear()
            from novafabric.registry.runs_cache import (  # noqa: PLC0415
                build_runs_index,
            )

            logger.warning(
                "capsule watcher event queue overflowed (maxsize=%d); "
                "degrading to a full incremental rescan of %s",
                self.QUEUE_MAX,
                capsule_dir,
            )
            inserted += build_runs_index(capsule_dir, conn, incremental=True)
        while True:
            try:
                p = self._queue.get_nowait()
            except queue.Empty:
                break
            # A watch event for a link (to a directory anywhere) is not a
            # capsule this server lists: iter_capsule_dirs' rule, per event.
            if p.is_symlink() or not is_capsule_regular_file(p, "capsule.yaml"):
                continue
            try:
                m = load_capsule_manifest(p)
            except (FileNotFoundError, yaml.YAMLError):
                continue
            upsert_run(conn, _manifest_to_summary(p, m))
            # ADR-0204 (experimental): fail-open content indexing on ingest.
            from novafabric.query.content_index import maybe_index_capsule  # noqa: PLC0415
            maybe_index_capsule(conn, p, str(m.get("run_id", p.name)))
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
        interval: float | None = None,
        backend: str | None = None,
    ) -> None:
        """*interval* / *backend* given explicitly win over the environment.

        ``None`` (the default) means "not given": ``NOVA_WATCHER_INTERVAL`` /
        ``NOVA_WATCHER_BACKEND`` apply, else ``2.0`` / ``auto``. Raises
        :class:`WatcherConfigError` for an unknown backend or a bad interval.
        """
        self._capsule_dir = capsule_dir
        self._db_path = db_path
        self.settings = resolve_watcher_settings(backend=backend, interval=interval)
        self._interval = self.settings.interval

        resolved = self.settings.backend
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
            if p.is_symlink():
                # A bare name is looked up like a scan: a linked entry in the
                # capsule dir is never indexed (an explicit path may be a link).
                return False, False
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
            # ADR-0204 (experimental): fail-open content indexing on ingest.
            from novafabric.query.content_index import maybe_index_capsule  # noqa: PLC0415
            maybe_index_capsule(conn, p, str(run_id))
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

    @property
    def interval(self) -> float:
        """The effective poll interval in seconds (flag > env > default)."""
        return self._interval

    def close(self) -> None:
        """Stop any OS-level watchers (WatchdogBackend only)."""
        self._backend.close()
