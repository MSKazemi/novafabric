"""End-to-end subprocess smoke test: spawn `nova serve --experimental --no-browser`,
hit /api/health, verify clean shutdown."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

pytest.importorskip("fastapi")
pytest.importorskip("uvicorn")


@pytest.mark.timeout(90)
def test_nova_serve_subprocess_starts_and_responds(tmp_path: Path) -> None:
    # Pick a port unlikely to collide with the user's running services
    port = 47312
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
        deadline = time.time() + 60
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
                f"nova serve did not respond within 8s: {last_err}\n"
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
