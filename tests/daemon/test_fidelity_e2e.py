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


def _wait_for(path: Path, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.1)
    return False


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
    daemon = subprocess.Popen(
        ["nova", "daemon", "start"], env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        assert _wait_for(sock), "daemon socket never appeared"
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
