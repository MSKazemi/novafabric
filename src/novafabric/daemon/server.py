"""Prefork AF_UNIX capture daemon.

The parent imports ``novafabric`` once (warm) and reads each request frame, then
``os.fork()``s one isolated worker per *capture* request. Bare connects (liveness
probes) and non-capture ops are answered without forking. Linux only (``os.fork``,
``SO_PEERCRED``).
"""

from __future__ import annotations

import os
import signal
import socket
import struct
from pathlib import Path
from types import FrameType

from novafabric.daemon.protocol import read_frame, write_frame
from novafabric.daemon.worker import run_worker

_SO_PEERCRED = getattr(socket, "SO_PEERCRED", 17)
_UCRED = struct.Struct("iII")  # pid, uid, gid


def peer_uid_ok(conn: socket.socket, allowed_uid: int) -> bool:
    raw = conn.getsockopt(socket.SOL_SOCKET, _SO_PEERCRED, _UCRED.size)
    _pid, uid, _gid = _UCRED.unpack(raw)
    return bool(uid == allowed_uid)


class CaptureDaemon:
    def __init__(
        self,
        socket_path: Path,
        max_concurrency: int = 64,
        allowed_uid: int | None = None,
    ) -> None:
        self.socket_path = Path(socket_path)
        self.max_concurrency = max_concurrency
        self.allowed_uid = os.getuid() if allowed_uid is None else allowed_uid
        self._sock: socket.socket | None = None
        self._active: set[int] = set()
        self._stop = False

    def bind(self) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.socket_path.parent, 0o700)
        if self.socket_path.exists():
            self.socket_path.unlink()
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(str(self.socket_path))
        os.chmod(self.socket_path, 0o600)
        sock.listen(128)
        self._sock = sock

    def _reap(self, _signum: int, _frame: FrameType | None) -> None:
        while True:
            try:
                pid, _status = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                break
            if pid == 0:
                break
            self._active.discard(pid)

    def _on_term(self, _signum: int, _frame: FrameType | None) -> None:
        self._stop = True
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def serve_forever(self) -> None:  # pragma: no cover
        # Blocking accept loop; exercised end-to-end via a real daemon subprocess
        # in tests/daemon/test_fidelity_e2e.py (not coverage-instrumented).
        assert self._sock is not None
        signal.signal(signal.SIGCHLD, self._reap)
        signal.signal(signal.SIGTERM, self._on_term)
        try:
            while not self._stop:
                try:
                    conn, _ = self._sock.accept()
                except InterruptedError:
                    continue
                except OSError:
                    break
                self._handle(conn)
        finally:
            self.close()

    def _handle(self, conn: socket.socket) -> None:
        try:
            if not peer_uid_ok(conn, self.allowed_uid):
                write_frame(conn, {"event": "error", "reason": "uid-denied"})
                conn.close()
                return
            request = read_frame(conn)
            if request is None:  # bare probe / disconnect → no fork
                conn.close()
                return
            if request.get("op") != "capture":
                write_frame(conn, {"event": "error", "reason": "bad-op"})
                conn.close()
                return
            if len(self._active) >= self.max_concurrency:
                write_frame(conn, {"event": "error", "reason": "busy"})
                conn.close()
                return
        except OSError:
            try:
                conn.close()
            except OSError:
                pass
            return

        pid = os.fork()
        if pid == 0:  # pragma: no cover  ---- worker child (separate process) ----
            code = 1
            try:
                if self._sock is not None:
                    self._sock.close()
                signal.signal(signal.SIGCHLD, signal.SIG_DFL)
                code = run_worker(conn, request)
            except BaseException:
                code = 1
            os._exit(code)
        # ---- parent ----
        self._active.add(pid)
        conn.close()

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        if self.socket_path.exists():
            self.socket_path.unlink()
