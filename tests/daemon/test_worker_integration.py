import os
import socket

from novafabric.daemon.protocol import read_frame, send_fds, write_frame
from novafabric.daemon.worker import run_worker


def test_worker_runs_command_and_writes_capsule(tmp_path, monkeypatch):
    """run_worker is forked-child-only (it dup2s stdio, setpgrp's, and may
    killpg its group). So we fork here: the child runs the worker; the parent
    verifies the exit frame and that a capsule was produced."""
    monkeypatch.setenv("NOVAFABRIC_HOME", str(tmp_path))

    parent_sock, child_sock = socket.socketpair()
    write_frame(
        parent_sock,
        {
            "op": "capture",
            "proto": 1,
            "argv": ["python", "-c", "print('hi')"],
            "cwd": str(tmp_path),
            "env": dict(os.environ),
        },
    )
    devnull = os.open(os.devnull, os.O_RDWR)
    send_fds(parent_sock, [devnull, devnull, devnull])

    pid = os.fork()
    if pid == 0:  # child: behave like the daemon's worker
        parent_sock.close()
        os.close(devnull)
        try:
            req = read_frame(child_sock)
            assert req is not None
            code = run_worker(child_sock, req)
        except BaseException:
            code = 1
        os._exit(code)

    # parent
    child_sock.close()
    os.close(devnull)
    _wpid, status = os.waitpid(pid, 0)
    assert os.WIFEXITED(status)
    assert os.WEXITSTATUS(status) == 0

    msg = read_frame(parent_sock)
    parent_sock.close()
    assert msg is not None
    assert msg["event"] == "exit"
    assert msg["code"] == 0
    assert "capsule_dir" in msg

    capsules = list((tmp_path / "capsules").glob("*"))
    assert len(capsules) == 1
