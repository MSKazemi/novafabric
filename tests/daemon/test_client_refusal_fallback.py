"""H4: a daemon that *refuses* a run must never block the user's workload.

The daemon answers ``{"event": "error", ...}`` only before it forks a worker
(``busy``, ``bad-op``, ``uid-denied``), so the workload has not started and the
client can safely run it directly via ``nova capture --no-daemon``. A daemon
that vanishes *after* accepting the run is different: the workload may already
be running, so the client must not start a second copy.
"""

from __future__ import annotations

import signal
import socket
import threading

import pytest

from novafabric.daemon import client
from novafabric.daemon.protocol import read_frame, recv_fds, write_frame


class _ExecSentinel(Exception):
    pass


def _serve_once(daemon_sock: socket.socket, reply: dict[str, object] | None) -> threading.Thread:
    def _run() -> None:
        read_frame(daemon_sock)
        recv_fds(daemon_sock, 3)
        if reply is not None:
            write_frame(daemon_sock, reply)
        daemon_sock.close()

    thread = threading.Thread(target=_run)
    thread.start()
    return thread


def _run_main(monkeypatch: pytest.MonkeyPatch, reply: dict[str, object] | None) -> tuple[object, dict[str, object]]:
    client_sock, daemon_sock = socket.socketpair()
    monkeypatch.setattr(client, "try_connect", lambda: client_sock)
    called: dict[str, object] = {}

    def fake_execvp(file: str, args: list[str]) -> None:
        called["file"] = file
        called["args"] = args
        raise _ExecSentinel

    monkeypatch.setattr(client.os, "execvp", fake_execvp)
    thread = _serve_once(daemon_sock, reply)
    outcome: object
    try:
        outcome = client.main(["python", "agent.py"])
    except _ExecSentinel:
        outcome = "exec"
    thread.join(timeout=5)
    client_sock.close()
    return outcome, called


@pytest.mark.parametrize("reason", ["busy", "bad-op", "uid-denied", "something-new"])
def test_refusal_falls_back_to_direct_capture(monkeypatch, capsys, reason):
    outcome, called = _run_main(monkeypatch, {"event": "error", "reason": reason})
    assert outcome == "exec", "a refused run must still execute the workload"
    assert called["file"] == "nova"
    assert called["args"] == ["nova", "capture", "--no-daemon", "python", "agent.py"]
    err = capsys.readouterr().err
    assert "novacap:" in err and reason in err
    assert "--no-daemon" in err


def test_uid_denied_warning_names_the_socket_override(monkeypatch, capsys):
    _run_main(monkeypatch, {"event": "error", "reason": "uid-denied"})
    assert "NOVAFABRIC_CAPTURE_SOCKET" in capsys.readouterr().err


def test_daemon_vanishing_mid_run_does_not_rerun_workload(monkeypatch, capsys):
    # EOF after the request was accepted: the workload may be running, so a
    # second copy would duplicate its side effects. Surface failure instead.
    outcome, called = _run_main(monkeypatch, None)
    assert outcome == 1
    assert called == {}
    assert "novacap:" in capsys.readouterr().err


def test_receive_error_mid_run_does_not_rerun_workload(monkeypatch):
    client_sock, daemon_sock = socket.socketpair()
    monkeypatch.setattr(client, "try_connect", lambda: client_sock)
    called: dict[str, object] = {}
    monkeypatch.setattr(client.os, "execvp", lambda f, a: called.setdefault("exec", a))

    def boom(_sock: socket.socket) -> None:
        raise ConnectionResetError("peer reset")

    monkeypatch.setattr(client, "_read_frame", boom)
    thread = _serve_once(daemon_sock, None)
    assert client.main(["python", "agent.py"]) == 1
    thread.join(timeout=5)
    client_sock.close()
    assert called == {}


def test_run_via_daemon_restores_signal_handlers(monkeypatch):
    before_int = signal.getsignal(signal.SIGINT)
    before_term = signal.getsignal(signal.SIGTERM)
    _run_main(monkeypatch, {"event": "exit", "code": 0})
    assert signal.getsignal(signal.SIGINT) is before_int
    assert signal.getsignal(signal.SIGTERM) is before_term
