"""Only one local pytest pool runs at a time — `scripts/test-pool.sh`.

Two full xdist pools at once have ended in SIGKILL and a reboot on the maintainers'
laptop, and concurrent sessions kept starting them because nothing told one session
that another was mid-run. `make test-fast`, the scoped runs and the push gate now run
pytest through `scripts/test-pool.sh`, which holds a machine-wide lock.

Each test uses its own lock file (NOVA_TEST_POOL_LOCK) and clears
NOVA_TEST_POOL_HELD, which is set when this very suite runs under the lock.
"""

from __future__ import annotations

import os
import pathlib
import re
import shutil
import subprocess
import time

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
POOL = REPO / "scripts" / "test-pool.sh"

pytestmark = pytest.mark.skipif(shutil.which("flock") is None, reason="needs flock (util-linux)")


def _env(lock: pathlib.Path, **extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("NOVA_TEST_POOL_")}
    env["NOVA_TEST_POOL_LOCK"] = str(lock)
    env.update(extra)
    return env


def _pool(lock: pathlib.Path, *cmd: str, **extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(POOL), *cmd],
        capture_output=True,
        text=True,
        check=False,
        env=_env(lock, **extra),
        timeout=60,
    )


def test_script_is_executable() -> None:
    assert os.access(POOL, os.X_OK), f"{POOL} is not executable"


def test_it_runs_the_command_and_returns_its_exit_status(tmp_path: pathlib.Path) -> None:
    ok = _pool(tmp_path / "lock", "bash", "-c", "echo ran; exit 0")
    assert ok.returncode == 0 and "ran" in ok.stdout
    bad = _pool(tmp_path / "lock", "bash", "-c", "exit 7")
    assert bad.returncode == 7


def test_a_second_pool_waits_and_gives_up_with_75(tmp_path: pathlib.Path) -> None:
    lock = tmp_path / "lock"
    holder = subprocess.Popen(
        ["bash", str(POOL), "sleep", "30"],
        env=_env(lock),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(100):  # wait until the holder has written who it is
            if (tmp_path / "lock.holder").exists() and (tmp_path / "lock.holder").read_text():
                break
            time.sleep(0.05)
        second = _pool(lock, "true", NOVA_TEST_POOL_WAIT="1")
    finally:
        holder.kill()
        holder.wait()
    assert second.returncode == 75, second.stderr
    assert "another test pool is running" in second.stderr
    assert re.search(r"pid \d+ since", second.stderr), "the waiting run did not say who holds it"


def test_a_nested_call_passes_straight_through(tmp_path: pathlib.Path) -> None:
    lock = tmp_path / "lock"
    inner = f"bash {POOL} echo inner-ran"
    out = _pool(lock, "bash", "-c", inner, NOVA_TEST_POOL_WAIT="1")
    assert out.returncode == 0, out.stderr
    assert "inner-ran" in out.stdout, "a nested test-pool call deadlocked on its own lock"


def test_the_command_does_not_inherit_the_lock(tmp_path: pathlib.Path) -> None:
    """A daemon a test leaves behind must not keep the lock held forever."""
    out = _pool(tmp_path / "lock", "bash", "-c", "ls /proc/self/fd")
    assert out.returncode == 0, out.stderr
    assert "9" not in out.stdout.split(), "fd 9 (the lock) leaked into the command"


def test_a_non_numeric_wait_is_refused(tmp_path: pathlib.Path) -> None:
    out = _pool(tmp_path / "lock", "true", NOVA_TEST_POOL_WAIT="soon")
    assert out.returncode == 2
    assert "not a number" in out.stderr


def test_off_runs_without_the_lock(tmp_path: pathlib.Path) -> None:
    out = _pool(tmp_path / "lock", "echo", "unlocked", NOVA_TEST_POOL_LOCK="off")
    assert out.returncode == 0 and "unlocked" in out.stdout


def test_every_local_pool_goes_through_the_lock() -> None:
    makefile = (REPO / "Makefile").read_text()
    recipe = re.search(r"^test-fast:\n\t(.*)$", makefile, re.MULTILINE)
    assert recipe and recipe.group(1).startswith("./scripts/test-pool.sh "), (
        "make test-fast no longer runs through scripts/test-pool.sh"
    )
    scoped = (REPO / "scripts" / "run-scoped-tests.sh").read_text()
    runs = re.findall(r"^\s*exec nice -n 10 (\S+)", scoped, re.MULTILINE)
    assert runs and all(r == "./scripts/test-pool.sh" for r in runs), runs
    gate = (REPO / "scripts" / "test-gate.sh").read_text()
    assert "./scripts/test-pool.sh $PYTEST" in gate, "the gate's partial tiers skip the lock"


def test_the_stop_hook_treats_a_busy_pool_as_a_skip_not_a_failure() -> None:
    hook = (REPO / "scripts" / "hooks" / "stop-scoped-tests.sh").read_text()
    assert "NOVA_TEST_POOL_WAIT=" in hook, "the Stop hook could wait on the lock forever"
    assert re.search(r"STATUS -eq 75 \]; then\n.*SKIPPED", hook), (
        "exit 75 (pool busy) would be reported to the model as failing tests"
    )
