"""Thin capture-daemon client.

STDLIB ONLY — importing this module must not pull in the ``novafabric`` package
(that would re-introduce the ~3 s cold-start this daemon exists to avoid). It
carries its own copies of the frame helpers. Console-script entry point:
``novacap``.

Never-block contract (ADR-0092): whenever the daemon cannot take a run — no
socket, unreachable, or it *refuses* the run (``busy``, ``bad-op``,
``uid-denied``) — the client runs the workload directly via
``nova capture --no-daemon``. A refusal always arrives before the daemon forks a
worker, so the fallback can never start a second copy of the workload. A daemon
that disappears *after* accepting a run is reported as a failure (exit 1) and the
workload is not re-run, because it may already have side effects.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import struct
import sys
from typing import Any

PROTO_VERSION = 1
_HEADER = struct.Struct(">I")


class DaemonRefused(Exception):
    """The daemon declined the run before forking a worker (workload not started)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def resolve_socket_path() -> str:
    env = os.environ.get("NOVAFABRIC_CAPTURE_SOCKET")
    if env:
        return env
    home = os.environ.get("NOVAFABRIC_HOME") or os.path.join(
        os.path.expanduser("~"), ".novafabric"
    )
    return os.path.join(home, "run", "capture.sock")


def build_fallback_argv(command: list[str]) -> list[str]:
    """Direct-spawn argv used when no daemon is reachable. ``--no-daemon``
    prevents `nova capture` from bouncing back to this client (no loop)."""
    return ["nova", "capture", "--no-daemon", *command]


def try_connect() -> socket.socket | None:
    path = resolve_socket_path()
    if not os.path.exists(path):
        return None
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(path)
        return sock
    except OSError:
        return None


def _write_frame(sock: socket.socket, obj: dict[str, Any]) -> None:
    body = json.dumps(obj, separators=(",", ":")).encode("utf-8")
    sock.sendall(_HEADER.pack(len(body)) + body)


def _read_frame(sock: socket.socket) -> dict[str, Any] | None:
    def _recv(n: int) -> bytes | None:
        buf = bytearray()
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                return None
            buf.extend(chunk)
        return bytes(buf)

    header = _recv(_HEADER.size)
    if header is None:
        return None
    (length,) = _HEADER.unpack(header)
    body = _recv(length)
    if body is None:
        return None
    result: dict[str, Any] = json.loads(body.decode("utf-8"))
    return result


def _warn(message: str) -> None:
    try:
        sys.stderr.write(f"novacap: {message}\n")
        sys.stderr.flush()
    except (OSError, ValueError):  # pragma: no cover - stderr closed
        pass


def _refusal_message(reason: str) -> str:
    message = (
        f"capture daemon refused the run ({reason}); "
        "running it directly via `nova capture --no-daemon`"
    )
    if reason == "uid-denied":
        message += (
            f". The socket {resolve_socket_path()} belongs to another user — "
            "check NOVAFABRIC_CAPTURE_SOCKET / NOVAFABRIC_HOME"
        )
    return message


def _send_request(sock: socket.socket, command: list[str]) -> None:
    request = {
        "op": "capture",
        "proto": PROTO_VERSION,
        "argv": command,
        "cwd": os.getcwd(),
        "env": dict(os.environ),
    }
    _write_frame(sock, request)
    socket.send_fds(sock, [b"\x00"], [0, 1, 2])  # stdin/stdout/stderr


def _await_exit(sock: socket.socket) -> int:
    """Read frames until the run ends and return its exit code.

    Raises :class:`DaemonRefused` when the daemon declined the run. Returns 1
    (and never re-runs) when the daemon goes away after accepting it.
    """

    def _forward(signum: int, _frame: object) -> None:
        try:
            _write_frame(sock, {"op": "signal", "signum": signum})
        except OSError:
            pass

    previous = {
        signal.SIGINT: signal.signal(signal.SIGINT, _forward),
        signal.SIGTERM: signal.signal(signal.SIGTERM, _forward),
    }
    try:
        while True:
            try:
                msg = _read_frame(sock)
            except (OSError, ValueError) as exc:
                _warn(f"lost the capture daemon mid-run ({exc}); not re-running the workload")
                return 1
            if msg is None:
                _warn("capture daemon closed the connection mid-run; not re-running the workload")
                return 1
            if msg.get("event") == "error":
                raise DaemonRefused(str(msg.get("reason") or "unspecified"))
            if msg.get("event") == "exit":
                return int(msg.get("code", 1))
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def _run_via_daemon(sock: socket.socket, command: list[str]) -> int:
    _send_request(sock, command)
    return _await_exit(sock)


def _close_quietly(sock: socket.socket) -> None:
    try:
        sock.close()
    except OSError:  # pragma: no cover
        pass


def main(argv: list[str] | None = None) -> int:
    command = list(sys.argv[1:] if argv is None else argv)
    if not command:
        sys.stderr.write("novacap: usage: novacap <command> [args...]\n")
        return 2
    sock = try_connect()
    if sock is None:
        os.execvp("nova", build_fallback_argv(command))  # pragma: no cover
    try:
        _send_request(sock, command)
    except (OSError, ValueError):  # pragma: no cover - request never delivered
        _close_quietly(sock)
        os.execvp("nova", build_fallback_argv(command))
    try:
        return _await_exit(sock)
    except DaemonRefused as refused:
        _warn(_refusal_message(refused.reason))
        _close_quietly(sock)
        os.execvp("nova", build_fallback_argv(command))
    raise AssertionError("unreachable: os.execvp never returns")  # pragma: no cover


if __name__ == "__main__":
    raise SystemExit(main())
