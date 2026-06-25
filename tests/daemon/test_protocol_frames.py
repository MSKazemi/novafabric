import socket

from novafabric.daemon.protocol import PROTO_VERSION, read_frame, write_frame


def test_frame_roundtrip_over_socketpair():
    a, b = socket.socketpair()
    write_frame(a, {"op": "capture", "proto": PROTO_VERSION, "argv": ["echo", "hi"]})
    msg = read_frame(b)
    assert msg is not None
    assert msg["op"] == "capture"
    assert msg["argv"] == ["echo", "hi"]
    a.close()
    b.close()


def test_read_frame_returns_none_on_clean_eof():
    a, b = socket.socketpair()
    a.close()
    assert read_frame(b) is None
    b.close()
