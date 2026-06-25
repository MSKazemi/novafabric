import os
import socket

from novafabric.daemon.protocol import recv_fds, send_fds


def test_fd_passing_roundtrip(tmp_path):
    target = tmp_path / "out.txt"
    f = open(target, "w")
    a, b = socket.socketpair()
    send_fds(a, [f.fileno()])
    got = recv_fds(b, 1)
    assert len(got) == 1
    os.write(got[0], b"hello-via-fd")
    os.close(got[0])
    f.close()
    a.close()
    b.close()
    assert target.read_text() == "hello-via-fd"
