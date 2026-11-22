"""The AWS event-stream binary framing (``application/vnd.amazon.eventstream``).

Bedrock ``ConverseStream`` answers with a sequence of messages, each framed as::

    total_length:u32  headers_length:u32  prelude_crc:u32
    headers           payload             message_crc:u32

(big-endian; both CRCs are CRC-32 -- the prelude's over the first 8 bytes, the
message's over everything before it). A header is ``name_len:u8 name type:u8
value``; only the string type (7) is used by the Bedrock runtime (``:event-type``,
``:content-type``, ``:message-type``, ``:exception-type``, ``:error-code``,
``:error-message``); other types are decoded so a frame is never mis-split.

Capture decodes the bytes the workload's own read delivered (:class:`Decoder`,
incremental, bounded); mocked replay encodes the recorded events back
(:func:`encode_message`). Pure functions and a small class; no I/O.
"""

from __future__ import annotations

import binascii
import struct
from dataclasses import dataclass

#: botocore refuses a payload above 24 MiB (``_MAX_PAYLOAD_LENGTH``) and headers
#: above 128 KiB; the same bounds here keep the buffer bounded on a body that is
#: not an event stream at all.
MAX_PAYLOAD_BYTES = 24 * 1024 * 1024
MAX_HEADERS_BYTES = 128 * 1024
_PRELUDE = 12
_MIN_MESSAGE = _PRELUDE + 4

_STRING = 7
#: header type -> fixed value length (``None``: 2-byte length prefix follows).
_FIXED: dict[int, int | None] = {
    0: 0, 1: 0,          # bool true / false
    2: 1, 3: 2, 4: 4, 5: 8,  # byte, short, integer, long
    6: None, 7: None,    # byte array, string
    8: 8, 9: 16,         # timestamp, uuid
}


class EventStreamError(ValueError):
    """The bytes are not a well-formed AWS event stream."""


@dataclass(frozen=True)
class Message:
    """One decoded frame. String headers decoded as text; others as raw bytes."""

    headers: dict[str, str | bytes | bool]
    payload: bytes


def _crc(data: bytes) -> int:
    return binascii.crc32(data) & 0xFFFFFFFF


def _decode_headers(raw: bytes) -> dict[str, str | bytes | bool]:
    headers: dict[str, str | bytes | bool] = {}
    at = 0
    while at < len(raw):
        name_len = raw[at]
        at += 1
        name = raw[at:at + name_len].decode("utf-8", "replace")
        at += name_len
        if at >= len(raw):
            raise EventStreamError("a header ends before its type")
        kind = raw[at]
        at += 1
        if kind not in _FIXED:
            raise EventStreamError(f"unknown header type {kind}")
        size = _FIXED[kind]
        if size is None:
            if at + 2 > len(raw):
                raise EventStreamError("a header ends before its length")
            (size,) = struct.unpack(">H", raw[at:at + 2])
            at += 2
        value = raw[at:at + size]
        if len(value) != size:
            raise EventStreamError("a header value is truncated")
        at += size
        if kind in (0, 1):
            headers[name] = kind == 0
        elif kind == _STRING:
            headers[name] = value.decode("utf-8", "replace")
        else:
            headers[name] = bytes(value)
    return headers


class Decoder:
    """Split a byte stream into :class:`Message` frames as bytes arrive.

    A malformed frame sets :attr:`failed` and stops decoding (nothing more is
    returned): capture then records why, never a guess.
    """

    def __init__(self) -> None:
        self._buffer = bytearray()
        self.failed: str | None = None

    @property
    def pending(self) -> int:
        """Bytes received that do not yet form a whole frame."""
        return len(self._buffer)

    def feed(self, data: bytes) -> list[Message]:
        if self.failed is not None or not data:
            return []
        self._buffer.extend(data)
        out: list[Message] = []
        try:
            while len(self._buffer) >= _PRELUDE:
                total, head_len, prelude_crc = struct.unpack(">III", self._buffer[:_PRELUDE])
                if _crc(bytes(self._buffer[:8])) != prelude_crc:
                    raise EventStreamError("prelude CRC mismatch")
                if head_len > MAX_HEADERS_BYTES:
                    raise EventStreamError("headers exceed the event-stream limit")
                if total < _MIN_MESSAGE + head_len:
                    raise EventStreamError("a frame is shorter than its headers")
                if total - _MIN_MESSAGE - head_len > MAX_PAYLOAD_BYTES:
                    raise EventStreamError("a payload exceeds the event-stream limit")
                if len(self._buffer) < total:
                    break
                frame = bytes(self._buffer[:total])
                del self._buffer[:total]
                (message_crc,) = struct.unpack(">I", frame[-4:])
                if _crc(frame[:-4]) != message_crc:
                    raise EventStreamError("message CRC mismatch")
                headers = _decode_headers(frame[_PRELUDE:_PRELUDE + head_len])
                out.append(Message(headers, frame[_PRELUDE + head_len:-4]))
        except (EventStreamError, struct.error) as exc:
            self.failed = f"the response body is not an AWS event stream ({exc})"
            self._buffer.clear()
        return out


def _encode_header(name: str, value: str) -> bytes:
    name_b, value_b = name.encode("utf-8"), value.encode("utf-8")
    if len(name_b) > 255 or len(value_b) > 0xFFFF:
        raise EventStreamError(f"header {name!r} is too long to encode")
    length = struct.pack(">H", len(value_b))
    return bytes([len(name_b)]) + name_b + bytes([_STRING]) + length + value_b


def encode_message(headers: list[tuple[str, str]], payload: bytes) -> bytes:
    """One event-stream frame with string *headers*, in order."""
    head = b"".join(_encode_header(k, v) for k, v in headers)
    total = _MIN_MESSAGE + len(head) + len(payload)
    prelude = struct.pack(">II", total, len(head))
    prelude += struct.pack(">I", _crc(prelude))
    body = prelude + head + payload
    return body + struct.pack(">I", _crc(body))
