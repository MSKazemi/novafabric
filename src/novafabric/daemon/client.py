"""Thin capture-daemon client.

STDLIB ONLY — importing this module must not pull in the ``novafabric`` package
(that would re-introduce the ~3 s cold-start this daemon exists to avoid). It
carries its own copies of the frame helpers. Console-script entry point:
``novacap``.
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


def _run_via_daemon(sock: socket.socket, command: list[str]) -> int:
    request = {
        "op": "capture",
        "proto": PROTO_VERSION,
        "argv": command,
        "cwd": os.getcwd(),
        "env": dict(os.environ),
    }
    _write_frame(sock, request)
    socket.send_fds(sock, [b"\x00"], [0, 1, 2])  # stdin/stdout/stderr

    def _forward(signum: int, _frame: object) -> None:
        try:
            _write_frame(sock, {"op": "signal", "signum": signum})
        except OSError:
            pass

    signal.signal(signal.SIGINT, _forward)
    signal.signal(signal.SIGTERM, _forward)

    while True:
        msg = _read_frame(sock)
        if msg is None:
            return 1  # daemon/worker vanished → surface failure
        if msg.get("event") == "error":
            return 1
        if msg.get("event") == "exit":
            return int(msg.get("code", 1))


def main(argv: list[str] | None = None) -> int:
    command = list(sys.argv[1:] if argv is None else argv)
    if not command:
        sys.stderr.write("novacap: usage: novacap <command> [args...]\n")
        return 2
    sock = try_connect()
    if sock is None:
        os.execvp("nova", build_fallback_argv(command))  # pragma: no cover
    try:
        return _run_via_daemon(sock, command)
    except (OSError, ValueError):  # pragma: no cover
        try:
            sock.close()
        except OSError:
            pass
        os.execvp("nova", build_fallback_argv(command))


if __name__ == "__main__":
    raise SystemExit(main())
