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

import pytest
import yaml
from typer.testing import CliRunner

from novafabric.cli.introspect import root_command, subcommands
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


# ── submit.sh, `nova validate`, and the documented limitation ───────────────

SUBMIT = EXAMPLE_DIR / "submit.sh"


def _bash() -> str:
    return shutil.which("bash") or "/bin/bash"


def _validate(capsule: Path) -> None:
    """`nova validate` accepts it — the issue's bar, not just `status: success`."""
    result = CliRunner().invoke(app, ["validate", str(capsule)])
    assert result.exit_code == 0, result.output


def test_the_local_capture_validates(tmp_path: Path) -> None:
    out = tmp_path / "capsules"
    out.mkdir()
    result = CliRunner().invoke(
        app,
        ["capture", "--output-dir", str(out), "--environment", "production",
         "--", sys.executable, str(PAYLOAD)],
        env={"NOVAFABRIC_EXAMPLE_OUT": str(tmp_path / "metrics.json")},
    )
    assert result.exit_code == 0, result.output
    _validate(_sole_capsule(out))


def test_submit_sh_is_valid_shell_and_executable() -> None:
    assert os.access(SUBMIT, os.X_OK), "submit.sh must be executable"
    proc = subprocess.run([_bash(), "-n", str(SUBMIT)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_submit_sh_skips_cleanly_without_sbatch(tmp_path: Path) -> None:
    """No scheduler on PATH is the CI case: exit 0 and say so, never fail."""
    # An empty PATH hides `sbatch`; bash itself is invoked by absolute path.
    env = {"PATH": "/nonexistent", "HOME": str(tmp_path)}
    proc = subprocess.run(
        [_bash(), str(SUBMIT)], capture_output=True, text=True, env=env, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("skip: 'sbatch' is not on PATH"), proc.stdout
    assert "job.sbatch" in proc.stdout  # points the reader at the no-scheduler path


def test_submit_sh_submits_job_sbatch_from_the_example_dir(tmp_path: Path) -> None:
    """With `sbatch` present, it is called on job.sbatch from the example dir.

    The directory matters: sbatch records it as SLURM_SUBMIT_DIR, which is
    where job.sbatch looks for payload.py. A stub stands in for the scheduler.
    """
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    record = tmp_path / "sbatch-call.txt"
    stub = stub_dir / "sbatch"
    stub.write_text(
        f'#!/bin/sh\necho "$PWD $*" > "{record}"\necho "Submitted batch job 7"\n'
    )
    stub.chmod(0o755)
    env = {"PATH": f"{stub_dir}:/usr/bin:/bin", "HOME": str(tmp_path)}
    proc = subprocess.run(
        [_bash(), str(SUBMIT)], capture_output=True, text=True, env=env, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert "Submitted batch job 7" in proc.stdout
    assert record.read_text().split() == [str(EXAMPLE_DIR), "job.sbatch"]


def test_the_batch_script_runs_as_a_plain_shell_script(tmp_path: Path) -> None:
    """README: `./job.sbatch` works with no scheduler and yields a valid capsule."""
    bin_dir = Path(sys.executable).parent
    if not (bin_dir / "nova").is_file():
        pytest.skip(f"no `nova` console script beside {sys.executable}")
    env = {k: v for k, v in os.environ.items() if not k.startswith("SLURM")}
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    env["NOVAFABRIC_CAPSULE_OUT"] = str(tmp_path / "capsules")
    env["NOVAFABRIC_EXAMPLE_OUT"] = str(tmp_path / "metrics.json")
    proc = subprocess.run(
        [_bash(), str(SBATCH)], capture_output=True, text=True, env=env,
        cwd=tmp_path, timeout=300,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "job.sbatch: capsules are under" in proc.stdout
    capsule = _sole_capsule(tmp_path / "capsules")
    manifest = yaml.safe_load((capsule / "capsule.yaml").read_text())
    assert manifest["deployment_environment"] == "production"
    _validate(capsule)


def _sbatch_capture_flags() -> list[str]:
    """The flags job.sbatch hands `nova capture`, up to the `--` separator."""
    logical = SBATCH.read_text().replace("\\\n", " ")
    line = next(
        ln for ln in logical.splitlines() if ln.strip().startswith("nova capture")
    )
    words = line.split()
    return [w for w in words[2 : words.index("--")] if w.startswith("-")]


def test_the_batch_script_only_uses_real_capture_flags() -> None:
    """Every flag job.sbatch passes is one `nova capture` declares.

    A renamed flag would otherwise surface only on a cluster, hours into a queue.
    """
    children = subcommands(root_command())
    assert children is not None
    capture = children["capture"]
    declared = {opt for p in capture.params for opt in (*p.opts, *p.secondary_opts)}
    flags = _sbatch_capture_flags()
    assert flags == ["--output-dir", "--environment"], flags
    assert set(flags) <= declared, set(flags) - declared


def test_the_capsule_records_no_slurm_context(tmp_path: Path) -> None:
    """Pins the README's "Not captured: any Slurm context at all".

    The job id, node and cluster reach the capsule only through the payload's
    own stdout. If NovaFabric starts recording scheduler context this fails —
    and the README section is then the thing to rewrite.
    """
    out = tmp_path / "capsules"
    out.mkdir()
    values = ("424242", "node-7", "cluster-x")
    result = CliRunner().invoke(
        app,
        ["capture", "--output-dir", str(out), "--", sys.executable, str(PAYLOAD)],
        env={
            "SLURM_JOB_ID": values[0],
            "SLURMD_NODENAME": values[1],
            "SLURM_CLUSTER_NAME": values[2],
            "NOVAFABRIC_EXAMPLE_OUT": str(tmp_path / "metrics.json"),
        },
    )
    assert result.exit_code == 0, result.output
    capsule = _sole_capsule(out)
    hits = sorted(
        p.relative_to(capsule).as_posix()
        for p in capsule.rglob("*")
        if p.is_file() and any(v in p.read_text(errors="ignore") for v in values)
    )
    assert hits == ["outputs/stdout.txt"], hits
