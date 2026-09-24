"""End-to-end subprocess smoke test: spawn `nova serve --experimental --no-browser`,
hit /api/health, verify clean shutdown."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

pytest.importorskip("fastapi")
pytest.importorskip("uvicorn")


# Single source for the startup budget. It was previously written as a literal in
# the deadline and a *different*, stale literal in the failure message: the budget
# went 8s -> 20s -> 60s while the message kept saying "8s", so a genuine failure
# under-reported its own budget by 7.5x and sent the reader looking for a fast
# timeout that no longer existed.
STARTUP_BUDGET_S = 60


@pytest.mark.timeout(90)
def test_nova_serve_subprocess_starts_and_responds(tmp_path: Path) -> None:
    # Ask the OS for a free port instead of hoping a fixed one is free.
    #
    # This was `port = 47312`, and a fixed port makes the test fail for a reason
    # that has nothing to do with the code under test: anything already bound
    # there -- a developer's own service, or a `nova serve` orphaned by a test run
    # that was killed rather than allowed to reach its `finally` block -- makes the
    # server fail to bind, and the client then gets ECONNREFUSED for the whole
    # budget. That reads identically to "the server is slow to start", which is
    # what the previous three timeout bumps were chasing.
    #
    # Binding port 0 and releasing it leaves a small race, but the window is
    # microseconds against a fixed port's window of "until someone notices".
    with socket.socket() as _s:
        _s.bind(("127.0.0.1", 0))
        port = _s.getsockname()[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent / "src")
    env["NOVAFABRIC_DB_PATH"] = str(tmp_path / "registry.db")
    env["NOVAFABRIC_DASHBOARD_AUDIT_FILE"] = str(tmp_path / "audit.jsonl")

    # Use a tmp capsule dir so we don't touch the user's real .novafabric/runs/
    capsule_dir = tmp_path / "runs"
    capsule_dir.mkdir()

    proc = subprocess.Popen(
        [
            sys.executable, "-m", "novafabric.cli.main", "serve",
            "--experimental",
            "--no-browser",
            "--port", str(port),
            "--capsule-dir", str(capsule_dir),
            "--db-path", str(tmp_path / "registry.db"),
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    try:
        # Wait for bind. The budget covers a cold interpreter start plus the app
        # import while the rest of the suite saturates every core.
        #
        # Raised twice now, and this time the reason was measured rather than
        # guessed. History: 8s failed under `make test-par`, so it became 20s. On
        # 2026-09-03 it still failed 2 of 4 full-suite runs. Re-measured on this
        # 20-core box: **3.2s idle, 16.8s under 20 synthetic CPU burners**. So 20s
        # was never comfortable, it was marginal — ~20% above the observed loaded
        # case — and the real suite, which also contends on I/O, memory and dozens
        # of interpreter starts, tips it over. That is a flake by arithmetic, not
        # bad luck, and bumping it without measuring would have been the third guess.
        #
        # 60s is ~3.5x the worst measured case and costs nothing on a healthy run:
        # the loop returns the moment the server binds, so a passing test is still
        # ~3s. The budget is only ever paid when something is genuinely wrong, and
        # the 90s @pytest.mark.timeout stays clear of it so a real hang still fails
        # by name rather than by budget.
        #
        # This is a smoke test — it asserts the CLI can start a server that answers
        # /api/health. It is not a startup-latency benchmark and must not fail
        # because the machine was busy.
        url = f"http://127.0.0.1:{port}/api/health"
        deadline = time.time() + STARTUP_BUDGET_S
        last_err: Exception | None = None
        while time.time() < deadline:
            try:
                r = httpx.get(url, timeout=1.0)
                if r.status_code == 200:
                    body = r.json()
                    assert body["ok"] is True
                    assert body["service"] == "nova-serve"
                    assert body["experimental"] is True
                    break
            except Exception as e:  # noqa: BLE001 — retry until timeout
                last_err = e
            time.sleep(0.2)
        else:
            stdout, stderr = proc.communicate(timeout=2)
            raise AssertionError(
                f"nova serve did not respond within {STARTUP_BUDGET_S}s "
                f"on port {port}: {last_err}\n"
                f"stdout: {stdout.decode(errors='replace')[:500]}\n"
                f"stderr: {stderr.decode(errors='replace')[:500]}"
            )

        # /api/runs without token must be 401
        r = httpx.get(f"http://127.0.0.1:{port}/api/runs", timeout=1.0)
        assert r.status_code == 401

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)
