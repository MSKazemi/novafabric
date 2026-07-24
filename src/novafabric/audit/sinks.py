"""Output sinks for audit-log follow mode — ADR-0191 D3 (experimental).

A sink is anything with ``write(str)`` / ``flush()`` / ``close()``, so
``follow_entries`` keeps taking a structural ``LineSink`` and stdout stays
the zero-configuration default. Two sinks live here:

- :class:`RotatingFileSink` — size- and generation-bounded file that a
  site's file shipper (Filebeat, Vector, Fluent Bit) picks up;
- :class:`SyslogSink` — RFC 5424 messages to a **local** syslog endpoint.

Socket posture (read this before changing it)
---------------------------------------------
Before :class:`SyslogSink`, this whole surface opened **no socket, ever**,
and a test asserted exactly that. That absolute property is now
**conditional**: nothing opens a socket unless an operator explicitly names
a syslog endpoint, and the endpoint is constrained *in code* to a Unix
socket or a **loopback** address — a non-loopback host is refused, not
merely discouraged.

This is the posture ADR-0191 D3 designed and D6 accepted (``socket`` is a
named allowed dependency); D6's alternatives section separately rejects
built-in *network* senders (Splunk HEC, Elastic bulk, Sentinel), and that
rejection still stands. Getting audit data off the box remains the site
syslog daemon's job — it already owns the TLS, retry and buffering
concerns we deliberately did not take on. The no-socket test still covers
stdout and file output, which are the defaults.

Why not ``logging.handlers``: ``RotatingFileHandler`` and ``SysLogHandler``
are *logging* handlers — they would drag a ``Logger``, its formatter and
process-global handler state into a path that is otherwise free of hidden
global state (a standing rule in this codebase), and ``SysLogHandler``
emits the older BSD RFC 3164 framing rather than the RFC 5424 D3 asks for.
Both policies here are small and directly testable.
"""

from __future__ import annotations

import ipaddress
import os
import socket
from datetime import datetime, timezone
from pathlib import Path
from types import TracebackType
from typing import Final

#: Refuse absurd configurations rather than silently producing a file per
#: line. Bounded-everything discipline: a sink that rotates on every write
#: is a disk-filling bug, not a configuration.
MIN_MAX_BYTES: Final[int] = 1024


class SinkError(Exception):
    """Raised for an invalid sink configuration."""


class RotatingFileSink:
    """Append lines to *path*, rotating at *max_bytes*, keeping *backup_count*.

    Rotation is the conventional numbered-suffix scheme: ``audit.cef``
    becomes ``audit.cef.1``, the previous ``.1`` becomes ``.2``, and the
    generation beyond *backup_count* is deleted. Rotation happens **before**
    a write that would cross the threshold, so a rendered record is never
    split across two files — a half-record is unparseable to the collector
    that reads it, which defeats the point of shipping it at all.

    With ``backup_count=0`` the file is truncated on rotation instead of
    being kept, which bounds disk use to *max_bytes* total.
    """

    def __init__(
        self, path: Path, *, max_bytes: int = 10 * 1024 * 1024, backup_count: int = 5
    ) -> None:
        if max_bytes < MIN_MAX_BYTES:
            raise SinkError(
                f"max_bytes must be at least {MIN_MAX_BYTES} (got {max_bytes})"
            )
        if backup_count < 0:
            raise SinkError(f"backup_count must not be negative (got {backup_count})")
        self._path = path
        self._max_bytes = max_bytes
        self._backup_count = backup_count
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Append: a restarted tailer must not destroy what it already shipped.
        self._fh = self._path.open("a", encoding="utf-8")
        self._size = self._path.stat().st_size
        self.rotations = 0

    def _rotate(self) -> None:
        self._fh.close()
        if self._backup_count == 0:
            self._path.unlink(missing_ok=True)
        else:
            # Walk down so a lower generation never overwrites a higher one.
            oldest = self._path.with_suffix(self._path.suffix + f".{self._backup_count}")
            oldest.unlink(missing_ok=True)
            for generation in range(self._backup_count - 1, 0, -1):
                src = self._path.with_suffix(self._path.suffix + f".{generation}")
                if src.exists():
                    src.rename(
                        self._path.with_suffix(self._path.suffix + f".{generation + 1}")
                    )
            if self._path.exists():
                self._path.rename(self._path.with_suffix(self._path.suffix + ".1"))
        self._fh = self._path.open("a", encoding="utf-8")
        self._size = 0
        self.rotations += 1

    def write(self, text: str) -> int:
        encoded_len = len(text.encode("utf-8"))
        # Rotate first so a record is never split across a boundary. A single
        # record larger than max_bytes still gets written whole — truncating
        # it would corrupt the stream — into its own freshly rotated file.
        if self._size and self._size + encoded_len > self._max_bytes:
            self._rotate()
        self._fh.write(text)
        self._size += encoded_len
        return len(text)

    def flush(self) -> None:
        self._fh.flush()
        os.fsync(self._fh.fileno())

    def close(self) -> None:
        if not self._fh.closed:
            self._fh.flush()
            self._fh.close()

    def __enter__(self) -> RotatingFileSink:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


# --- RFC 5424 local syslog sink (ADR-0191 D3) --------------------------------

#: Syslog facility. local0 is the conventional "site-local application"
#: facility; nothing here should land in the kernel or auth facilities.
DEFAULT_FACILITY: Final[int] = 16  # local0

#: Severity 6 = Informational. Same stance as the OCSF `severity_id` and the
#: constant CEF severity: NovaFabric states what happened; how alarming that
#: is, is the SIEM's policy call.
_SEVERITY_INFO: Final[int] = 6

#: RFC 5424 caps: APP-NAME 48, MSGID 32 octets.
_APP_NAME_MAX: Final[int] = 48
_MSGID_MAX: Final[int] = 32

#: Conservative datagram budget. RFC 5424 requires receivers to accept 480
#: octets and recommends 2048; going far above that invites silent drops in
#: the syslog daemon, which is the worst outcome for an audit stream.
DEFAULT_MAX_DATAGRAM_BYTES: Final[int] = 2048

#: Appended when a message must be shortened to fit a datagram. Visible on
#: purpose — a silently shortened audit record is indistinguishable from a
#: complete one, which would be a false negative in an investigation.
TRUNCATION_MARKER: Final[str] = "…[NOVAFABRIC-TRUNCATED]"

_LOOPBACK_HOSTS: Final[frozenset[str]] = frozenset(
    {"localhost", "127.0.0.1", "::1", "ip6-localhost"}
)


def _is_loopback(host: str) -> bool:
    if host in _LOOPBACK_HOSTS:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class SyslogSink:
    """Render lines as RFC 5424 messages to a **local** syslog endpoint.

    ADR-0191 D3 scopes this to "a user-configured Unix socket or loopback
    UDP/TCP address", and D6's alternatives section rejects built-in network
    senders outright. That constraint is enforced **in code**, not merely
    documented: a non-loopback host is refused. Shipping off-box remains the
    job of the site's own syslog daemon, which already owns the TLS, retry
    and buffering concerns we deliberately did not take on.

    There is **no default endpoint** — this sink exists only when an operator
    names one.

    Framing is rendered here rather than by ``logging.handlers.SysLogHandler``
    because that handler emits the older BSD (RFC 3164) format.

    Transports: ``unix`` (datagram, falling back to stream — ``/dev/log``
    varies by platform), ``udp``, and ``tcp`` with RFC 6587 octet counting so
    a stream receiver can find message boundaries.
    """

    def __init__(
        self,
        address: str,
        *,
        transport: str = "auto",
        facility: int = DEFAULT_FACILITY,
        app_name: str = "novafabric",
        msgid: str = "audit",
        max_datagram_bytes: int = DEFAULT_MAX_DATAGRAM_BYTES,
    ) -> None:
        if transport not in ("auto", "unix", "udp", "tcp"):
            raise SinkError(
                f"unknown syslog transport {transport!r} (expected unix|udp|tcp)"
            )
        if not 0 <= facility <= 23:
            raise SinkError(f"syslog facility must be 0–23 (got {facility})")

        self._address = address
        self._facility = facility
        self._app_name = app_name[:_APP_NAME_MAX] or "-"
        self._msgid = msgid[:_MSGID_MAX] or "-"
        self._max_datagram_bytes = max_datagram_bytes
        self._hostname = socket.gethostname()[:255] or "-"
        self._pid = os.getpid()
        self.messages_sent = 0
        self.messages_truncated = 0

        self._transport = self._resolve_transport(address, transport)
        self._host = ""
        self._port = 0

        if self._transport != "unix":
            self._host, self._port = self._split_host_port(address)
            if not _is_loopback(self._host):
                raise SinkError(
                    f"syslog host {self._host!r} is not loopback. ADR-0191 D3 "
                    "scopes this sink to a LOCAL endpoint (unix socket or "
                    "loopback UDP/TCP); shipping off-box is the site syslog "
                    "daemon's job, not NovaFabric's."
                )

        self._stream = False
        self._sock = self._connect()

    @staticmethod
    def _resolve_transport(address: str, transport: str) -> str:
        if transport != "auto":
            return transport
        return "unix" if address.startswith("/") else "udp"

    @staticmethod
    def _split_host_port(address: str) -> tuple[str, int]:
        if address.startswith("["):  # [::1]:514
            host, _, rest = address[1:].partition("]")
            port_text = rest.lstrip(":")
        else:
            host, _, port_text = address.rpartition(":")
        if not host or not port_text:
            raise SinkError(
                f"syslog address {address!r} must be host:port or a /unix/socket path"
            )
        try:
            return host, int(port_text)
        except ValueError as exc:
            raise SinkError(f"syslog port in {address!r} is not a number") from exc

    def _connect_unix(self) -> socket.socket:
        # /dev/log is SOCK_DGRAM on Linux and SOCK_STREAM on some systems;
        # try datagram first, then stream, rather than assuming.
        for kind in (socket.SOCK_DGRAM, socket.SOCK_STREAM):
            sock = socket.socket(socket.AF_UNIX, kind)
            try:
                sock.connect(self._address)
            except OSError:
                sock.close()
                continue
            self._stream = kind == socket.SOCK_STREAM
            return sock
        raise SinkError(f"cannot reach syslog unix socket {self._address!r}")

    def _connect_inet(self, host: str, port: int) -> socket.socket:
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        kind = socket.SOCK_STREAM if self._transport == "tcp" else socket.SOCK_DGRAM
        sock = socket.socket(family, kind)
        try:
            sock.connect((host, port))
        except OSError as exc:
            sock.close()
            raise SinkError(
                f"cannot reach syslog endpoint {self._address!r}: {exc}"
            ) from exc
        self._stream = kind == socket.SOCK_STREAM
        return sock

    def _connect(self) -> socket.socket:
        if self._transport == "unix":
            return self._connect_unix()
        host, port = self._host, self._port
        return self._connect_inet(host, port)

    def _header(self) -> str:
        pri = self._facility * 8 + _SEVERITY_INFO
        ts = datetime.now(tz=timezone.utc).isoformat(timespec="microseconds")
        ts = ts.replace("+00:00", "Z")
        return (
            f"<{pri}>1 {ts} {self._hostname} {self._app_name} "
            f"{self._pid} {self._msgid} -"
        )

    def _frame(self, text: str) -> bytes:
        message = f"{self._header()} {text.rstrip(chr(10))}"
        encoded = message.encode("utf-8")

        if not self._stream and len(encoded) > self._max_datagram_bytes:
            # Shorten the MSG, never the header, and mark it — a silently
            # shortened audit record would read as a complete one.
            marker = TRUNCATION_MARKER.encode("utf-8")
            encoded = encoded[: self._max_datagram_bytes - len(marker)] + marker
            self.messages_truncated += 1

        if self._stream:
            # RFC 6587 octet counting, so a stream receiver finds boundaries.
            return str(len(encoded)).encode("ascii") + b" " + encoded
        return encoded

    def write(self, text: str) -> int:
        if not text.strip():
            return 0
        self._sock.sendall(self._frame(text))
        self.messages_sent += 1
        return len(text)

    def flush(self) -> None:
        """No-op: every write is already sent — sockets are not buffered here."""

    def close(self) -> None:
        self._sock.close()

    def __enter__(self) -> SyslogSink:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
