"""LocalRunner waits for the workload to exit, not for its pipes, and bounds what it keeps.

Review capture-runners-10: ``communicate(timeout)`` waits for EOF on stdout/stderr.
A workload that starts a detached daemon (``setsid``, double-fork) exits at once, but
the daemon inherits the pipes: the deadline fired, ``killpg`` missed the escaped
child, and a second, unbounded ``communicate()`` blocked until that child exited — a
successful run recorded as ``exit_code: 124`` after the daemon's whole lifetime. All
output was also held in memory with no cap.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from novafabric.runners import LocalRunner, RunnerJobSpec, _local


def _spec(tmp_path: Path, script: str, timeout_s: float = 30.0) -> RunnerJobSpec:
    return RunnerJobSpec(
        run_id="01TESTOUTPUT00000000000000", command=["sh", "-c", script],
        capsule_dir=tmp_path, env={"PATH": os.environ["PATH"]}, timeout_s=timeout_s,
    )


def test_a_detached_child_holding_stdout_does_not_turn_success_into_a_timeout(
    tmp_path: Path,
) -> None:
    pidfile = tmp_path / "daemon.pid"
    script = f"setsid sh -c 'echo $$ > {pidfile}; exec sleep 30' & echo started"
    t0 = time.monotonic()
    try:
        result = LocalRunner().run(_spec(tmp_path, script, timeout_s=20.0))
        elapsed = time.monotonic() - t0
    finally:
        if pidfile.exists():
            try:
                os.kill(int(pidfile.read_text()), 15)
            except (ProcessLookupError, ValueError):
                pass
    assert (result.exit_code, result.runner_status) == (0, "completed")
    assert result.stdout.startswith(b"started\n")
    assert b"still holds" in result.stderr  # says why output may be incomplete
    assert elapsed < 15, f"took {elapsed:.1f}s: waited for the detached child"


def test_the_timeout_is_enforced_on_the_workload_itself(tmp_path: Path) -> None:
    t0 = time.monotonic()
    result = LocalRunner().run(_spec(tmp_path, "sleep 30", timeout_s=1.0))
    assert (result.exit_code, result.runner_status) == (124, "timeout")
    assert time.monotonic() - t0 < 10


def test_output_beyond_the_cap_is_counted_not_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_local, "OUTPUT_CAP_BYTES", 1000)
    result = LocalRunner().run(_spec(tmp_path, "yes aaaaaaaaa | head -c 5000"))
    assert result.runner_status == "completed"
    assert result.stdout[:1000] == (b"aaaaaaaaa\n" * 100)
    assert b"4000 byte(s) of stdout not kept" in result.stdout
    assert result.runner_metadata["stdout_truncated_bytes"] == 4000


def test_ordinary_output_is_unchanged(tmp_path: Path) -> None:
    result = LocalRunner().run(_spec(tmp_path, "echo out; echo err >&2; exit 3"))
    assert (result.exit_code, result.stdout, result.stderr) == (3, b"out\n", b"err\n")
    assert "stdout_truncated_bytes" not in result.runner_metadata
