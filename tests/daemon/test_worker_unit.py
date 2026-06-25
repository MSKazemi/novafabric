import os
import signal
import socket
import threading
import time

from novafabric.daemon import worker as worker_mod
from novafabric.daemon.protocol import write_frame


def test_apply_env_replaces_environ(monkeypatch):
    saved = dict(os.environ)
    try:
        worker_mod._apply_env({"FOO": "bar"})
        assert os.environ.get("FOO") == "bar"
        assert "PATH" not in os.environ or os.environ.get("PATH") is not None
    finally:
        os.environ.clear()
        os.environ.update(saved)


def test_cancel_watcher_clean_exit_no_killpg(monkeypatch):
    killed = []
    monkeypatch.setattr(worker_mod.os, "killpg", lambda pg, sig: killed.append(sig))
    a, b = socket.socketpair()
    done = threading.Event()
    t = worker_mod._start_cancel_watcher(b, done)
    time.sleep(0.1)
    done.set()  # run finished cleanly
    t.join(timeout=3)
    assert killed == []  # never signalled the group on a clean finish
    a.close()
    b.close()


def test_cancel_watcher_signals_group_on_signal_frame(monkeypatch):
    killed = []
    monkeypatch.setattr(worker_mod.os, "killpg", lambda pg, sig: killed.append(sig))
    monkeypatch.setattr(worker_mod, "_GRACE_KILL_S", 0.0)
    a, b = socket.socketpair()
    done = threading.Event()
    t = worker_mod._start_cancel_watcher(b, done)
    write_frame(a, {"op": "signal", "signum": int(signal.SIGINT)})
    t.join(timeout=3)
    assert int(signal.SIGINT) in killed
    assert int(signal.SIGKILL) in killed  # escalation after grace
    a.close()
    b.close()


def test_cancel_watcher_signals_group_on_client_disconnect(monkeypatch):
    killed = []
    monkeypatch.setattr(worker_mod.os, "killpg", lambda pg, sig: killed.append(sig))
    monkeypatch.setattr(worker_mod, "_GRACE_KILL_S", 0.0)
    a, b = socket.socketpair()
    done = threading.Event()
    t = worker_mod._start_cancel_watcher(b, done)
    a.close()  # client vanished → EOF → cancel
    t.join(timeout=3)
    assert int(signal.SIGTERM) in killed
    b.close()
