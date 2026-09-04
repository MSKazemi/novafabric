"""End-to-end fidelity gate (ADR-0092).

Proves a capsule produced via the warm daemon (`novacap`) is structurally
identical to one from a direct `nova capture --no-daemon`, modulo volatile
fields. Starts a real daemon subprocess; always tears it down.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest
import yaml

VOLATILE = {"run_id", "created_at", "finished_at", "duration_ms", "capsule_id"}

_HAVE_CLI = shutil.which("nova") is not None and shutil.which("novacap") is not None


def _normalize(capsule_dir: Path) -> dict:
    data = yaml.safe_load((capsule_dir / "capsule.yaml").read_text())
    return {k: v for k, v in data.items() if k not in VOLATILE}


def _wait_for_socket(
    path: Path, proc: subprocess.Popen, log: Path, timeout: float = 60.0
) -> None:
    """Block until the daemon binds its socket, or fail with real diagnostics.

    The timeout is generous because the suite runs under `pytest -n auto`, where a
    fresh interpreter plus imports takes far longer than on an idle machine; 15s
    was enough alone and failed reproducibly under load. Polling `proc.poll()`
    keeps that generosity cheap -- a daemon that actually dies fails immediately
    instead of burning the whole timeout, so "slow" stays distinguishable from
    "crashed" rather than collapsing into one unexplained assertion.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        if proc.poll() is not None:
            raise AssertionError(
                f"daemon exited with code {proc.returncode} before binding {path}\n"
                f"--- daemon output ---\n{log.read_text(errors='replace')}"
            )
        time.sleep(0.1)
    raise AssertionError(
        f"daemon socket {path} never appeared within {timeout:.0f}s "
        f"(process still alive={proc.poll() is None})\n"
        f"--- daemon output ---\n{log.read_text(errors='replace')}"
    )


@pytest.mark.skipif(not _HAVE_CLI, reason="nova/novacap console scripts not installed")
def test_daemon_capsule_matches_direct(tmp_path):
    env = {**os.environ, "NOVAFABRIC_HOME": str(tmp_path)}
    env.pop("NOVAFABRIC_CAPTURE_SOCKET", None)
    workload = ["python", "-c", "print('fidelity')"]

    # 1) direct (in-process), same cwd so working_directory matches
    subprocess.run(
        ["nova", "capture", "--no-daemon", *workload],
        check=True, env=env, cwd=str(tmp_path),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    direct_dirs = sorted((tmp_path / "capsules").glob("*"))
    assert len(direct_dirs) == 1

    # 2) start the daemon, run the same workload via novacap
    sock = tmp_path / "run" / "capture.sock"
    # Kept on disk rather than DEVNULL: it is the only way a failure here can say
    # why the daemon never came up.
    daemon_log = tmp_path / "daemon.log"
    log_fh = daemon_log.open("wb")
    daemon = subprocess.Popen(
        ["nova", "daemon", "start"], env=env,
        stdout=log_fh, stderr=subprocess.STDOUT,
    )
    try:
        _wait_for_socket(sock, daemon, daemon_log)
        subprocess.run(
            ["novacap", *workload],
            check=True, env=env, cwd=str(tmp_path),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60,
        )
    finally:
        daemon.terminate()
        try:
            daemon.wait(timeout=10)
        except subprocess.TimeoutExpired:
            daemon.kill()
        log_fh.close()

    all_dirs = sorted((tmp_path / "capsules").glob("*"))
    assert len(all_dirs) == 2, f"expected 2 capsules, got {len(all_dirs)}"
    daemon_dir = next(d for d in all_dirs if d not in direct_dirs)

    direct_norm = _normalize(direct_dirs[0])
    daemon_norm = _normalize(daemon_dir)

    # Same structural shape and the behavioral fields match.
    assert set(direct_norm.keys()) == set(daemon_norm.keys())
    assert direct_norm["command"] == daemon_norm["command"] == list(workload)
    assert direct_norm["capture_mode"] == daemon_norm["capture_mode"] == "cli-wrapper"
    assert direct_norm["exit_code"] == daemon_norm["exit_code"] == 0
    assert direct_norm["model_call_count"] == daemon_norm["model_call_count"]
    assert direct_norm["working_directory"] == daemon_norm["working_directory"]
