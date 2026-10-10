"""End-to-end regression test for examples/docker-run.

The example's claim is that `nova capture --runner docker` produces a capsule of
a containerized run, and that the capsule shows the *container's* interpreter in
its captured output while its environment lock describes the *host*. Both halves
are asserted here, because the second is a limitation the README states plainly
and a test that only checked the happy half would let that statement rot.

Docker is absent from CI and from many first-time clones, so everything below
skips cleanly rather than failing. The skip is deliberate and narrow: it covers
"no docker" only, never a docker run that failed.
"""

from __future__ import annotations

import functools
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

import pytest
import yaml
from typer.testing import CliRunner

from novafabric.cli.main import app

EXAMPLE_DIR = Path(__file__).resolve().parent.parent / "examples" / "docker-run"
IMAGE = "python:3.12-slim"


def _image_whose_python_differs_from_the_host() -> str:
    """A stock image whose Python minor version cannot equal the host's.

    `IMAGE` deliberately matches the tag the README tells readers to run, so the
    rest of this module exercises the documented path. One test, however, proves
    the capsule locks the *host* environment rather than the container's, and it
    can only do so while the two report different versions.

    Hardcoding a second tag re-runs the bug that made this necessary: the
    example pinned `python:3.12-slim` and GitHub's runner image moved to Python
    3.12.14, so container and host reported the identical version and the test
    could no longer tell them apart. Choosing the tag relative to the running
    interpreter makes that collision unreachable instead of merely unlikely.
    """
    host_minor = sys.version_info.minor
    for minor in (13, 12, 11):
        if minor != host_minor:
            return f"python:3.{minor}-slim"
    raise AssertionError(f"no contrasting tag for host Python 3.{host_minor}")

_F = TypeVar("_F", bound=Callable[..., None])


@functools.lru_cache(maxsize=1)
def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        proc = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


# The daemon tier is a marker plus a fixture, deliberately *not* a module-level
# `skipif`: a `skipif` condition is evaluated during collection, so every pytest
# invocation in this repo — including the ones that never select this file —
# paid a `docker info` subprocess with a 10 s timeout before it could collect
# anything. `-m "not container"` now drops these tests without probing the
# daemon at all, and the fixture still skips cleanly when the tier *is* selected
# on a machine without Docker.
@pytest.fixture()
def _require_docker_daemon() -> None:
    """Skip, at setup time, if the daemon is unreachable.

    Same narrow contract the module docstring states: this covers "no docker"
    only, never a docker run that failed. Deliberately not ``autouse``: two
    tests in this module assert things about the example's *files* and must keep
    running on a machine with no Docker at all.
    """
    if not _docker_available():
        pytest.skip("docker daemon not reachable")
    from _docker_image import skip_if_image_rate_limited

    skip_if_image_rate_limited(IMAGE)  # a registry rate limit is "no image", not a failed run


def requires_docker(func: _F) -> _F:
    """Put a test in the Docker-daemon tier."""
    return pytest.mark.container(pytest.mark.usefixtures("_require_docker_daemon")(func))


def _sole_capsule(out_dir: Path) -> Path:
    """The one capsule under *out_dir*.

    Selected by the presence of `capsule.yaml`, not by "the only directory":
    the hermetic-env fixture drops a `.nova-home-hermetic/` beside it, so
    counting directories picks up a decoy.
    """
    capsules = [d for d in out_dir.iterdir() if d.is_dir() and (d / "capsule.yaml").is_file()]
    assert len(capsules) == 1, capsules
    return capsules[0]


def test_the_example_files_exist() -> None:
    """Runs everywhere, including without docker — the example must be complete."""
    assert (EXAMPLE_DIR / "payload.py").is_file()
    assert (EXAMPLE_DIR / "README.md").is_file()


def test_payload_is_stdlib_only_and_runs_anywhere() -> None:
    """The payload must need nothing but the standard library."""
    source = (EXAMPLE_DIR / "payload.py").read_text()
    for banned in ("import requests", "import numpy", "import torch", "openai"):
        assert banned not in source, f"payload.py must stay stdlib-only, found {banned}"


@requires_docker
def test_capture_in_a_container_produces_a_valid_capsule(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "capture",
            "--output-dir", str(tmp_path),
            "--runner", "docker",
            "--runner-option", f"image={IMAGE}",
            "--runner-option", "workdir=/work",
            "--runner-option", f"extra_volumes={EXAMPLE_DIR}:/work:ro",
            "python", "/work/payload.py",
        ],
    )
    assert result.exit_code == 0, f"capture failed:\n{result.output}"

    capsule = _sole_capsule(tmp_path)

    manifest = yaml.safe_load((capsule / "capsule.yaml").read_text())
    assert manifest["status"] == "success"
    assert manifest["exit_code"] == 0

    stdout = (capsule / "outputs" / "stdout.txt").read_text()
    # The container's interpreter, not the host's — this is what makes it a
    # containerized capture rather than a local one wearing a flag.
    assert "payload: hello from the container" in stdout
    assert "payload: python   = 3.12." in stdout, stdout
    # The capsule dir the workload saw was the in-container path.
    assert "payload: capsule  = /novafabric/capsule" in stdout, stdout


@requires_docker
def test_the_environment_lock_describes_the_host_not_the_container(
    tmp_path: Path,
) -> None:
    """Pins the limitation the README states, so the statement cannot go stale.

    If NovaFabric ever starts locking the *container's* environment, this test
    fails — and the README section it guards is then the thing to update. That
    is the intended outcome, not a nuisance: a documented limitation with no test
    is a sentence that quietly stops being true.
    """
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "capture",
            "--output-dir", str(tmp_path),
            "--runner", "docker",
            "--runner-option", f"image={_image_whose_python_differs_from_the_host()}",
            "--runner-option", "workdir=/work",
            "--runner-option", f"extra_volumes={EXAMPLE_DIR}:/work:ro",
            "python", "/work/payload.py",
        ],
    )
    assert result.exit_code == 0, result.output
    capsule = _sole_capsule(tmp_path)

    manifest = yaml.safe_load((capsule / "capsule.yaml").read_text())
    stdout = (capsule / "outputs" / "stdout.txt").read_text()

    container_python = next(
        line.split("=", 1)[1].strip()
        for line in stdout.splitlines()
        if line.startswith("payload: python")
    )
    host_python = str(manifest["host"]["python"])
    assert container_python != host_python, (
        f"container and host both report Python {container_python}, so this "
        "test cannot tell which environment the capsule locked. The image is "
        "chosen to differ from the host minor version, so reaching this means "
        "_image_whose_python_differs_from_the_host() picked a colliding tag."
    )


@requires_docker
def test_extra_volumes_from_the_cli_actually_reaches_docker(tmp_path: Path) -> None:
    """Regression: --runner-option extra_volumes= used to be silently discarded.

    The CLI can only produce strings, and the coercion accepted lists only, so the
    mount vanished with no error and the container failed to find its payload.
    This asserts the fix through the real CLI surface, which is where it broke.
    """
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "capture",
            "--output-dir", str(tmp_path),
            "--runner", "docker",
            "--runner-option", f"image={IMAGE}",
            "--runner-option", "workdir=/work",
            "--runner-option", f"extra_volumes={EXAMPLE_DIR}:/work:ro",
            "python", "/work/payload.py",
        ],
    )
    # Without the mount the container cannot see /work/payload.py at all, so
    # the interpreter exits 2 with "can't open file" on stderr.
    assert result.exit_code == 0, result.output
    capsule = _sole_capsule(tmp_path)
    stdout = (capsule / "outputs" / "stdout.txt").read_text()
    assert "payload: hello from the container" in stdout, (
        "the mount did not reach docker — the payload never ran"
    )
    # stderr.txt is only written when the workload wrote to stderr, so its
    # absence is the success case here and must not read as a missing file.
    stderr_path = capsule / "outputs" / "stderr.txt"
    stderr = stderr_path.read_text() if stderr_path.is_file() else ""
    assert "No such file or directory" not in stderr, stderr


# ── run.sh: the documented entry point ──────────────────────────────────────
#
# Most of what run.sh promises is checkable with no Docker at all: that it skips
# with exit 0 when Docker is missing or the daemon is down, and exactly which
# `docker run` it causes NovaFabric to issue. A stub `docker` on PATH records the
# argv; it never pretends to be a container, and no test below claims a
# containerized run from it.

RUN_SH = EXAMPLE_DIR / "run.sh"

_STUB_DOCKER = """#!/bin/sh
if [ "$1" = info ]; then
  if [ -n "${STUB_DOCKER_DOWN:-}" ]; then
    echo "Cannot connect to the Docker daemon" >&2
    exit 1
  fi
  echo 28.0.0
  exit 0
fi
if [ "$1" = run ]; then
  printf '%s\\n' "$@" > "$STUB_DOCKER_RECORD"
  echo "stub: docker run recorded"
  exit 0
fi
if [ "$1" = image ] && [ "$2" = inspect ]; then
  echo "sha256:$STUB_IMAGE_HEX docker.io/library/python@sha256:$STUB_REPO_HEX"
  exit 0
fi
exit 2
"""
_STUB_IMAGE_HEX = "1" * 64
_STUB_REPO_HEX = "2" * 64


def _bash() -> str:
    return shutil.which("bash") or "/bin/bash"


def _run_sh_with_stub(tmp_path: Path, **extra_env: str) -> subprocess.CompletedProcess[str]:
    bin_dir = Path(sys.executable).parent
    if not (bin_dir / "nova").is_file():
        pytest.skip(f"no `nova` console script beside {sys.executable}")
    stub_dir = tmp_path / "stub-bin"
    stub_dir.mkdir()
    (stub_dir / "docker").write_text(_STUB_DOCKER)
    (stub_dir / "docker").chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{stub_dir}:{bin_dir}:/usr/bin:/bin",
        "STUB_DOCKER_RECORD": str(tmp_path / "docker-argv.txt"),
        "STUB_IMAGE_HEX": _STUB_IMAGE_HEX,
        "STUB_REPO_HEX": _STUB_REPO_HEX,
        **extra_env,
    }
    return subprocess.run(
        [_bash(), str(RUN_SH), str(tmp_path / "capsules")],
        capture_output=True, text=True, env=env, timeout=300,
    )


def test_run_sh_is_valid_shell_and_executable() -> None:
    assert os.access(RUN_SH, os.X_OK), "run.sh must be executable"
    proc = subprocess.run([_bash(), "-n", str(RUN_SH)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_run_sh_skips_cleanly_without_docker(tmp_path: Path) -> None:
    """No `docker` binary: exit 0 and say so — the CI and fresh-clone case."""
    env = {"PATH": "/nonexistent", "HOME": str(tmp_path)}
    proc = subprocess.run(
        [_bash(), str(RUN_SH), str(tmp_path / "capsules")],
        capture_output=True, text=True, env=env, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("skip: 'docker' is not on PATH"), proc.stdout
    assert not (tmp_path / "capsules").exists(), "a skip must not start a capture"


def test_run_sh_skips_cleanly_when_the_daemon_is_down(tmp_path: Path) -> None:
    proc = _run_sh_with_stub(tmp_path, STUB_DOCKER_DOWN="1")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("skip: the Docker daemon is not reachable"), proc.stdout
    assert not (tmp_path / "docker-argv.txt").exists(), "no container may be started"


def test_run_sh_issues_the_documented_unprivileged_docker_run(tmp_path: Path) -> None:
    """The exact `docker run` behind the README's command, Docker or not.

    Pins what the README states about the container: it runs as the invoking
    user, not root; nothing privileged; the example dir is mounted read-only;
    and the submitting shell's environment does NOT cross into the container
    (ADR-0270) — only NovaFabric's own variables and PYTHONPATH do.
    """
    proc = _run_sh_with_stub(tmp_path, EXAMPLE_HOST_SECRET="do-not-forward")
    assert proc.returncode == 0, proc.stdout + proc.stderr

    argv = (tmp_path / "docker-argv.txt").read_text().splitlines()
    assert argv[:2] == ["run", "--rm"], argv
    assert argv[-3:] == ["python:3.12-slim", "python", "/work/payload.py"], argv

    def value_of(flag: str) -> list[str]:
        return [argv[i + 1] for i, a in enumerate(argv[:-1]) if a == flag]

    assert value_of("--user") == [f"{os.getuid()}:{os.getgid()}"]
    assert value_of("--workdir") == ["/work"]
    volumes = value_of("-v")
    assert f"{EXAMPLE_DIR}:/work:ro" in volumes, volumes
    assert any(v.endswith(":/novafabric/capsule") for v in volumes), volumes
    for forbidden in ("--privileged", "--pid", "--ipc", "--cap-add"):
        assert forbidden not in argv, forbidden

    env_keys = {e.split("=", 1)[0] for e in value_of("-e")}
    assert "EXAMPLE_HOST_SECRET" not in env_keys
    assert "do-not-forward" not in "\n".join(argv)
    assert env_keys <= {"PYTHONPATH"} | {k for k in env_keys if k.startswith("NOVAFABRIC_")}
    assert "NOVAFABRIC_CAPSULE_DIR=/novafabric/capsule" in value_of("-e")


def test_run_sh_capsule_records_the_runner_and_the_resolved_digest(tmp_path: Path) -> None:
    """Pins the README's "What identifies the container run" (ADR-0307).

    The stub answers `docker image inspect` the way the daemon does; the digest
    in the capsule must be the one the runtime reported, never the tag.
    """
    proc = _run_sh_with_stub(tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    capsule = _sole_capsule(tmp_path / "capsules")
    result = CliRunner().invoke(app, ["validate", str(capsule)])
    assert result.exit_code == 0, result.output

    runner = yaml.safe_load((capsule / "capsule.yaml").read_text())["host"]["runner"]
    assert runner == {
        "name": "docker",
        "image": {
            "reference": "python:3.12-slim",
            "resolved_by": "docker-image-inspect",
            "image_id": f"sha256:{_STUB_IMAGE_HEX}",
            "repo_digests": [f"docker.io/library/python@sha256:{_STUB_REPO_HEX}"],
        },
    }


@requires_docker
def test_run_sh_produces_a_capsule_that_validates(tmp_path: Path) -> None:
    """The README's documented command, end to end against a real daemon."""
    proc = subprocess.run(
        [_bash(), str(RUN_SH), str(tmp_path / "capsules")],
        capture_output=True, text=True, timeout=600,
        env={**os.environ, "PATH": f"{Path(sys.executable).parent}:{os.environ['PATH']}"},
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    capsule = _sole_capsule(tmp_path / "capsules")
    result = CliRunner().invoke(app, ["validate", str(capsule)])
    assert result.exit_code == 0, result.output

    stdout = (capsule / "outputs" / "stdout.txt").read_text()
    assert "payload: hello from the container" in stdout

    # Pins the README's "a stock image cannot run wire-level capture": the hook
    # loader is mounted and runs, but python:3.12-slim has no NovaFabric to
    # import, and it says so on stderr. If this stops appearing, either the
    # image gained NovaFabric or the loader stopped running — re-read the README.
    stderr = (capsule / "outputs" / "stderr.txt").read_text()
    assert "hook install failed" in stderr, stderr
