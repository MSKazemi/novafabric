"""End-to-end regression test for examples/hpc-slurm-job.

The example's constraint, stated in its own README, is that it runs on a machine
with **no Slurm installed** — which is exactly the machine CI runs on. So the
no-scheduler path is tested unconditionally and the `sbatch` path skips cleanly.

One test here guards a defect the example actually had: `dirname "$0"` inside a
batch script resolves to Slurm's per-job spool directory, not to the submission
directory, so the payload was not found. That was caught on a real cluster and
would not have been caught by reading the script.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import yaml
from typer.testing import CliRunner

from novafabric.cli.main import app

EXAMPLE_DIR = Path(__file__).resolve().parent.parent / "examples" / "hpc-slurm-job"
PAYLOAD = EXAMPLE_DIR / "payload.py"
SBATCH = EXAMPLE_DIR / "job.sbatch"


def _sole_capsule(out_dir: Path) -> Path:
    """The one capsule under *out_dir*, chosen by `capsule.yaml`.

    Not "the only directory": the hermetic-env fixture leaves a
    `.nova-home-hermetic/` beside it, which would be counted as a decoy.
    """
    found = [d for d in out_dir.iterdir() if d.is_dir() and (d / "capsule.yaml").is_file()]
    assert len(found) == 1, found
    return found[0]


def test_the_example_files_exist_and_the_script_is_executable() -> None:
    assert PAYLOAD.is_file()
    assert SBATCH.is_file()
    assert (EXAMPLE_DIR / "README.md").is_file()
    assert os.access(SBATCH, os.X_OK), "job.sbatch must be executable to run locally"


def test_payload_is_stdlib_only() -> None:
    """No torch, no GPU, no key — the example must run in three seconds anywhere."""
    source = PAYLOAD.read_text()
    for banned in ("import torch", "import numpy", "import requests", "openai"):
        assert banned not in source, f"payload.py must stay stdlib-only, found {banned}"


def test_payload_runs_without_a_scheduler(tmp_path: Path) -> None:
    """The README promises `python3 payload.py` works with no Slurm present."""
    env = dict(os.environ)
    env["NOVAFABRIC_EXAMPLE_OUT"] = str(tmp_path / "metrics.json")
    for key in list(env):
        if key.startswith("SLURM"):
            del env[key]

    proc = subprocess.run(
        [sys.executable, str(PAYLOAD)],
        capture_output=True, text=True, env=env, cwd=tmp_path, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert "scheduler= none (running locally)" in proc.stdout, proc.stdout
    assert (tmp_path / "metrics.json").is_file()


def test_payload_reports_slurm_context_when_the_scheduler_sets_it(
    tmp_path: Path,
) -> None:
    """The capsule records no Slurm context, so the payload must print it.

    That is the workaround the README documents. If it stops working, the
    documented pattern is broken and the README is the thing to fix.
    """
    env = dict(os.environ)
    env["NOVAFABRIC_EXAMPLE_OUT"] = str(tmp_path / "metrics.json")
    env["SLURM_JOB_ID"] = "424242"
    env["SLURM_JOB_NAME"] = "novafabric-example"
    env["SLURMD_NODENAME"] = "node-7"

    proc = subprocess.run(
        [sys.executable, str(PAYLOAD)],
        capture_output=True, text=True, env=env, cwd=tmp_path, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert "scheduler= slurm" in proc.stdout
    assert "SLURM_JOB_ID = 424242" in proc.stdout
    assert "SLURMD_NODENAME = node-7" in proc.stdout


def test_capture_of_the_payload_produces_a_valid_capsule(tmp_path: Path) -> None:
    """The capture pattern itself, without any scheduler involved."""
    out = tmp_path / "capsules"
    out.mkdir()
    result = CliRunner().invoke(
        app,
        ["capture", "--output-dir", str(out), "--environment", "production",
         "--", sys.executable, str(PAYLOAD)],
    )
    assert result.exit_code == 0, result.output

    capsule = _sole_capsule(out)
    manifest = yaml.safe_load((capsule / "capsule.yaml").read_text())
    assert manifest["status"] == "success"
    assert manifest["exit_code"] == 0

    stdout = (capsule / "outputs" / "stdout.txt").read_text()
    assert "payload: wrote" in stdout


def test_the_batch_script_does_not_use_dirname_for_the_payload_path() -> None:
    """Regression guard for the defect a real cluster exposed.

    Slurm copies the batch script to a per-job spool dir on the compute node, so
    resolving the payload from the script's own location finds nothing. The
    script must prefer SLURM_SUBMIT_DIR. Asserted against the text because the
    failure only reproduces under a real scheduler, which CI does not have — a
    guard that cannot run is worth less than one that reads the source.
    """
    source = SBATCH.read_text()
    assert "SLURM_SUBMIT_DIR" in source, (
        "job.sbatch must resolve the payload from SLURM_SUBMIT_DIR; "
        "dirname \"$0\" points at Slurm's spool directory inside a job"
    )
    submit_dir_line = next(
        i for i, line in enumerate(source.splitlines())
        if 'SCRIPT_DIR="${SLURM_SUBMIT_DIR}"' in line
    )
    fallback_line = next(
        i for i, line in enumerate(source.splitlines())
        if 'SCRIPT_DIR="$(cd "$(dirname' in line
    )
    assert submit_dir_line < fallback_line, (
        "SLURM_SUBMIT_DIR must be preferred; the dirname form is the "
        "no-scheduler fallback only"
    )


def test_the_batch_script_is_valid_shell() -> None:
    """A syntax error here would only surface on a cluster, hours later."""
    if shutil.which("bash") is None:  # pragma: no cover - bash is everywhere
        return
    proc = subprocess.run(["bash", "-n", str(SBATCH)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
