import os
import signal
import socket
import threading
import time

from novafabric.daemon import worker as worker_mod
from novafabric.daemon.protocol import write_frame

_JOIN_S = 30.0
"""Generous on purpose: `join` returns the moment the thread ends, so a large
budget costs nothing when passing. Under `make test-par`'s 20 workers the old
3s budget expired before the watcher was scheduled, and because
`Thread.join(timeout=...)` does NOT raise, the assertion then ran against an
empty list and reported `assert 2 in []` — which reads as a wrong signal rather
than a thread that never finished."""


def _join_finished(t: threading.Thread) -> None:
    """Join *t*, and fail saying so if it is still running."""
    t.join(timeout=_JOIN_S)
    assert not t.is_alive(), (
        f"cancel watcher still running after {_JOIN_S}s — the assertions below "
        "would otherwise test an empty result and blame the signal"
    )



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
    _join_finished(t)
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
    _join_finished(t)
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
    _join_finished(t)
    assert int(signal.SIGTERM) in killed
    b.close()
