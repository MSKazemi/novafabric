"""The AWS event-stream codec capture decodes with and replay encodes with.

botocore's own ``EventStreamBuffer`` is the oracle: what we encode it must decode to
the same headers and payload, and what it accepts we must decode the same way --
whatever the chunking of the bytes.
"""

from __future__ import annotations

import binascii
import json
import struct

import pytest

from novafabric.capture.hooks._aws_eventstream import (
    MAX_PAYLOAD_BYTES,
    Decoder,
    EventStreamError,
    encode_message,
)

eventstream = pytest.importorskip("botocore.eventstream")

HEADERS = [(":event-type", "contentBlockDelta"), (":content-type", "application/json"),
           (":message-type", "event")]
PAYLOAD = json.dumps({"contentBlockIndex": 0, "delta": {"text": "héllo"}}).encode()


def _botocore_decode(data: bytes) -> list[tuple[dict[str, object], bytes]]:
    buffer = eventstream.EventStreamBuffer()
    buffer.add_data(data)
    return [(m.headers, m.payload) for m in buffer]


def test_botocore_decodes_what_we_encode() -> None:
    frames = encode_message(HEADERS, PAYLOAD) + encode_message(HEADERS[:1], b"{}")
    decoded = _botocore_decode(frames)
    assert decoded == [(dict(HEADERS), PAYLOAD), ({":event-type": "contentBlockDelta"}, b"{}")]


@pytest.mark.parametrize("step", [1, 3, 7, 64, 10_000])
def test_we_decode_frames_split_at_any_boundary(step: int) -> None:
    data = encode_message(HEADERS, PAYLOAD) * 3
    decoder = Decoder()
    messages = []
    for at in range(0, len(data), step):
        messages.extend(decoder.feed(data[at:at + step]))
    assert decoder.failed is None and decoder.pending == 0
    assert [(m.headers, m.payload) for m in messages] == [(dict(HEADERS), PAYLOAD)] * 3


def test_non_string_headers_are_decoded_not_mis_split() -> None:
    # An int32 header (type 4) and a bool (type 0) ahead of a string header.
    head = b"\x03int" + b"\x04" + struct.pack(">i", 7) + b"\x02ok" + b"\x00"
    head += b"\x01s" + b"\x07" + struct.pack(">H", 2) + b"hi"
    total = 16 + len(head) + 2
    prelude = struct.pack(">II", total, len(head))
    prelude += struct.pack(">I", binascii.crc32(prelude) & 0xFFFFFFFF)
    body = prelude + head + b"{}"
    frame = body + struct.pack(">I", binascii.crc32(body) & 0xFFFFFFFF)
    (message,) = Decoder().feed(frame)
    assert message.headers == {"int": struct.pack(">i", 7), "ok": True, "s": "hi"}
    assert [h for h, _ in _botocore_decode(frame)][0]["s"] == "hi"


@pytest.mark.parametrize("where", ["prelude", "message"])
def test_a_corrupted_frame_stops_decoding(where: str) -> None:
    frame = bytearray(encode_message(HEADERS, PAYLOAD))
    frame[5 if where == "prelude" else -6] ^= 0xFF
    decoder = Decoder()
    assert decoder.feed(bytes(frame)) == []
    assert decoder.failed is not None and "CRC" in decoder.failed
    assert decoder.feed(encode_message(HEADERS, PAYLOAD)) == [], "decoded past a failure"


def test_a_body_that_is_not_an_event_stream_is_refused_without_buffering_it() -> None:
    decoder = Decoder()
    decoder.feed(json.dumps({"message": "not a stream"}).encode())
    assert decoder.failed is not None and decoder.pending == 0


def test_an_oversized_payload_is_refused_before_it_is_buffered() -> None:
    total = 16 + MAX_PAYLOAD_BYTES + 1
    prelude = struct.pack(">II", total, 0)
    prelude += struct.pack(">I", binascii.crc32(prelude) & 0xFFFFFFFF)
    decoder = Decoder()
    decoder.feed(prelude)
    assert decoder.failed is not None and "limit" in decoder.failed


def test_an_unencodable_header_raises() -> None:
    with pytest.raises(EventStreamError):
        encode_message([("x" * 300, "v")], b"")
