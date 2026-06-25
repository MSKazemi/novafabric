"""Post-fork capture worker.

``run_worker`` MUST be called only in a forked child: it rebinds stdio onto the
caller's passed fds, creates a fresh process group, and may signal that group on
cancellation. It runs the existing ``CaptureOrchestrator`` verbatim, so a capsule
produced via the daemon is identical to one from a direct ``nova capture``.
"""

from __future__ import annotations

import os
import select
import signal
import socket
import threading
import time
from typing import Any

from novafabric._paths import default_capsule_dir
from novafabric.capture.orchestrator import CaptureOrchestrator
from novafabric.daemon.protocol import read_frame, recv_fds, write_frame

_GRACE_KILL_S = 5.0


def _apply_env(env: dict[str, str]) -> None:
    os.environ.clear()
    os.environ.update(env)


def _start_cancel_watcher(conn: socket.socket, done: threading.Event) -> threading.Thread:
    """Watch the control socket. On an inbound ``{op:"signal"}`` frame or a
    client disconnect (EOF) — and only while the run is still active — terminate
    this worker's process group (SIGTERM, then SIGKILL after a grace period).
    Never fires once ``done`` is set, so a cleanly finished run is untouched."""

    def _watch() -> None:
        while not done.is_set():
            try:
                ready, _, _ = select.select([conn], [], [], 0.2)
            except OSError:
                return
            if not ready:
                continue
            try:
                msg = read_frame(conn)
            except OSError:
                msg = None
            if done.is_set():
                return
            signum: int = int(signal.SIGTERM)
            if msg and msg.get("op") == "signal":
                signum = int(msg.get("signum", signal.SIGTERM))
            try:
                os.killpg(os.getpgrp(), signum)
                time.sleep(_GRACE_KILL_S)
                os.killpg(os.getpgrp(), signal.SIGKILL)
            except OSError:
                pass
            return

    thread = threading.Thread(target=_watch, daemon=True)
    thread.start()
    return thread


def run_worker(conn: socket.socket, request: dict[str, Any]) -> int:  # pragma: no cover
    """Execute one capture run; return its exit code. Forked-child only.

    Not coverage-instrumented because it runs only inside a forked child (it
    rebinds stdio, ``setpgrp``s, and may ``killpg`` its group) — it is exercised
    for real via a fork in ``tests/daemon/test_worker_integration.py`` and the
    end-to-end ``tests/daemon/test_fidelity_e2e.py``.
    """
    command = list(request["argv"])
    cwd = request.get("cwd") or os.getcwd()
    env = request.get("env") or dict(os.environ)

    fds = recv_fds(conn, 3)
    if len(fds) == 3:
        for target, src in zip((0, 1, 2), fds, strict=True):
            os.dup2(src, target)
        for src in fds:
            if src not in (0, 1, 2):
                os.close(src)

    _apply_env(env)
    try:
        os.chdir(cwd)
    except OSError:
        pass
    os.setpgrp()  # isolate the workload's process group for clean cancellation

    done = threading.Event()
    _start_cancel_watcher(conn, done)

    orch = CaptureOrchestrator(base_dir=default_capsule_dir())
    try:
        result = orch.run(command)
    finally:
        done.set()

    write_frame(
        conn,
        {
            "event": "exit",
            "code": int(result.exit_code),
            "run_id": result.run_id,
            "capsule_dir": str(result.capsule_dir),
        },
    )
    return int(result.exit_code)
