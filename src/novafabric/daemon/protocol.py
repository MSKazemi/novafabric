"""Wire protocol for the capture daemon.

Length-prefixed JSON frames plus SCM_RIGHTS file-descriptor passing. Stdlib
only. Used by the server and worker; the thin client (``client.py``) carries its
own stdlib copies of the frame helpers so that importing it never pulls in the
``novafabric`` package.
"""

from __future__ import annotations

import json
import socket
import struct
from typing import Any

from novafabric.daemon import PROTO_VERSION

__all__ = ["PROTO_VERSION", "write_frame", "read_frame", "send_fds", "recv_fds"]

_HEADER = struct.Struct(">I")  # 4-byte big-endian length prefix


def write_frame(sock: socket.socket, obj: dict[str, Any]) -> None:
    """Serialize ``obj`` as JSON and send it as one length-prefixed frame."""
    body = json.dumps(obj, separators=(",", ":")).encode("utf-8")
    sock.sendall(_HEADER.pack(len(body)) + body)


def _recv_exactly(sock: socket.socket, n: int) -> bytes | None:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


def read_frame(sock: socket.socket) -> dict[str, Any] | None:
    """Read one frame. Returns ``None`` on clean EOF (peer closed)."""
    header = _recv_exactly(sock, _HEADER.size)
    if header is None:
        return None
    (length,) = _HEADER.unpack(header)
    body = _recv_exactly(sock, length)
    if body is None:
        return None
    result: dict[str, Any] = json.loads(body.decode("utf-8"))
    return result


def send_fds(sock: socket.socket, fds: list[int]) -> None:
    """Send file descriptors over a unix socket via SCM_RIGHTS.

    A single 1-byte payload carries the ancillary control message.
    """
    socket.send_fds(sock, [b"\x00"], fds)


def recv_fds(sock: socket.socket, max_fds: int) -> list[int]:
    """Receive up to ``max_fds`` descriptors; returns the list of new fds."""
    _msg, fds, _flags, _addr = socket.recv_fds(sock, 1, max_fds)
    return list(fds)
