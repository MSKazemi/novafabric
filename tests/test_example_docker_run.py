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

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from novafabric.cli.main import app

EXAMPLE_DIR = Path(__file__).resolve().parent.parent / "examples" / "docker-run"
IMAGE = "python:3.12-slim"


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


requires_docker = pytest.mark.skipif(
    not _docker_available(), reason="docker daemon not reachable"
)


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
            "--runner-option", f"image={IMAGE}",
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
        "the container and host happen to run the same Python, so this test "
        "cannot distinguish them — pin a different image tag"
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
