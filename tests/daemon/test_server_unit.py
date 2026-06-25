import os
import socket

from novafabric.daemon.server import CaptureDaemon, peer_uid_ok


def test_peer_uid_ok_accepts_same_uid():
    a, b = socket.socketpair()
    assert peer_uid_ok(a, allowed_uid=os.getuid()) is True
    a.close()
    b.close()


def test_peer_uid_ok_rejects_other_uid():
    a, b = socket.socketpair()
    assert peer_uid_ok(a, allowed_uid=os.getuid() + 12345) is False
    a.close()
    b.close()


def test_daemon_binds_socket_with_0600(tmp_path):
    sock_path = tmp_path / "run" / "capture.sock"
    d = CaptureDaemon(socket_path=sock_path, max_concurrency=4)
    d.bind()
    try:
        assert sock_path.exists()
        assert (sock_path.parent.stat().st_mode & 0o777) == 0o700
        assert (sock_path.stat().st_mode & 0o777) == 0o600
    finally:
        d.close()
    assert not sock_path.exists()  # close() unlinks


def test_bind_replaces_stale_socket_file(tmp_path):
    sock_path = tmp_path / "run" / "capture.sock"
    sock_path.parent.mkdir(parents=True)
    sock_path.write_text("stale")  # leftover file from a previous daemon
    d = CaptureDaemon(socket_path=sock_path)
    d.bind()
    try:
        assert sock_path.exists()
        assert (sock_path.stat().st_mode & 0o777) == 0o600
    finally:
        d.close()
