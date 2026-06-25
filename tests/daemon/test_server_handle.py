import os
import socket

from novafabric.daemon import server as server_mod
from novafabric.daemon.protocol import read_frame, write_frame
from novafabric.daemon.server import CaptureDaemon


def _daemon(tmp_path, **kw):
    return CaptureDaemon(socket_path=tmp_path / "run" / "capture.sock", **kw)


def test_handle_rejects_other_uid(tmp_path, monkeypatch):
    monkeypatch.setattr(server_mod, "peer_uid_ok", lambda *_a, **_k: False)
    d = _daemon(tmp_path)
    a, b = socket.socketpair()
    d._handle(a)
    msg = read_frame(b)
    assert msg["event"] == "error"
    assert msg["reason"] == "uid-denied"
    b.close()


def test_handle_bad_op(tmp_path, monkeypatch):
    monkeypatch.setattr(server_mod, "peer_uid_ok", lambda *_a, **_k: True)
    d = _daemon(tmp_path)
    a, b = socket.socketpair()
    write_frame(b, {"op": "nonsense"})
    d._handle(a)
    msg = read_frame(b)
    assert msg["event"] == "error"
    assert msg["reason"] == "bad-op"
    b.close()


def test_handle_busy_when_at_capacity(tmp_path, monkeypatch):
    monkeypatch.setattr(server_mod, "peer_uid_ok", lambda *_a, **_k: True)
    d = _daemon(tmp_path, max_concurrency=0)  # always "busy" before any fork
    a, b = socket.socketpair()
    write_frame(b, {"op": "capture", "argv": ["x"]})
    d._handle(a)
    msg = read_frame(b)
    assert msg["event"] == "error"
    assert msg["reason"] == "busy"
    b.close()


def test_handle_probe_disconnect_no_fork(tmp_path, monkeypatch):
    monkeypatch.setattr(server_mod, "peer_uid_ok", lambda *_a, **_k: True)
    forked = {"count": 0}
    monkeypatch.setattr(server_mod.os, "fork", lambda: forked.__setitem__("count", 1))
    d = _daemon(tmp_path)
    a, b = socket.socketpair()
    b.close()  # bare probe: client closes without sending a frame
    d._handle(a)
    assert forked["count"] == 0  # read_frame None → no fork


def test_handle_forks_for_capture(tmp_path, monkeypatch):
    monkeypatch.setattr(server_mod, "peer_uid_ok", lambda *_a, **_k: True)
    # Pretend to be the parent side of fork (returns a pid, child branch skipped).
    monkeypatch.setattr(server_mod.os, "fork", lambda: 4242)
    d = _daemon(tmp_path)
    a, b = socket.socketpair()
    write_frame(b, {"op": "capture", "argv": ["echo"]})
    d._handle(a)
    assert 4242 in d._active
    b.close()


def test_on_term_sets_stop(tmp_path):
    d = _daemon(tmp_path)
    d.bind()
    d._on_term(15, None)
    assert d._stop is True
    assert d._sock is None
    d.close()


def test_reap_discards_exited_child(tmp_path):
    import time

    d = _daemon(tmp_path)
    pid = os.fork()
    if pid == 0:
        os._exit(0)
    d._active.add(pid)
    # Poll _reap until the child is collected (avoids a WNOHANG timing race).
    for _ in range(100):
        d._reap(17, None)
        if pid not in d._active:
            break
        time.sleep(0.02)
    assert pid not in d._active
