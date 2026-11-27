"""The example entry scripts find their own directory without coreutils (issue #209).

`examples/hpc-slurm-job/submit.sh` and `examples/notebook-capture/run.sh` print
paths inside their own directory in the skip message they show when the
scheduler or Jupyter is missing. They used to compute that directory with
`dirname`, a coreutils binary, so on a PATH without coreutils the "clean skip"
died with `dirname: command not found` instead. `examples/docker-run/run.sh`
had the same pattern; its tests live in `tests/test_example_docker_run.py`.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"

# (script, the tool whose absence makes it skip, the skip line it prints first)
SCRIPTS = [
    pytest.param(
        EXAMPLES / "hpc-slurm-job" / "submit.sh",
        "sbatch",
        "skip: 'sbatch' is not on PATH",
        id="hpc-slurm-job/submit.sh",
    ),
    pytest.param(
        EXAMPLES / "notebook-capture" / "run.sh",
        "jupyter",
        "skip: 'jupyter' is not on PATH",
        id="notebook-capture/run.sh",
    ),
]

_SYSTEM_PATH = "/usr/bin:/bin"


def _bash() -> str:
    return shutil.which("bash") or "/bin/bash"


def _run(
    script: Path, path_env: str, tmp_path: Path, cwd: Path | None = None, arg0: str | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [_bash(), arg0 or str(script), str(tmp_path / "out")],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=cwd,
        env={"PATH": path_env, "HOME": str(tmp_path)},
    )


@pytest.mark.parametrize(("script", "tool", "skip_line"), SCRIPTS)
def test_skips_cleanly_with_no_coreutils_on_path(
    script: Path, tool: str, skip_line: str, tmp_path: Path
) -> None:
    proc = _run(script, "/nonexistent", tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith(skip_line), proc.stdout
    assert "command not found" not in proc.stderr, proc.stderr
    # The paths it prints point into its own directory.
    assert str(script.parent) in proc.stdout, proc.stdout


@pytest.mark.parametrize(("script", "tool", "skip_line"), SCRIPTS)
def test_skips_cleanly_when_invoked_by_bare_name(
    script: Path, tool: str, skip_line: str, tmp_path: Path
) -> None:
    """`bash submit.sh` from the example dir: a path with no slash at all."""
    proc = _run(script, "/nonexistent", tmp_path, cwd=script.parent, arg0=script.name)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith(skip_line), proc.stdout
    assert str(script.parent) in proc.stdout, proc.stdout


@pytest.mark.parametrize(("script", "tool", "skip_line"), SCRIPTS)
def test_a_symlinked_script_resolves_to_the_real_example_dir(
    script: Path, tool: str, skip_line: str, tmp_path: Path
) -> None:
    if shutil.which(tool, path=_SYSTEM_PATH):
        pytest.skip(f"{tool} is installed on {_SYSTEM_PATH}, so the script would not skip")
    if not shutil.which("readlink", path=_SYSTEM_PATH):
        pytest.skip(f"no readlink on {_SYSTEM_PATH}")
    first = tmp_path / "bin" / "entry"
    first.parent.mkdir()
    first.symlink_to(script)
    second = tmp_path / "bin" / "entry-again"
    second.symlink_to(first.name)  # a relative link to a link
    proc = _run(script, _SYSTEM_PATH, tmp_path, arg0=str(second))
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith(skip_line), proc.stdout
    assert str(script.parent) in proc.stdout, proc.stdout
    assert str(first.parent) not in proc.stdout, proc.stdout


@pytest.mark.parametrize(("script", "tool", "skip_line"), SCRIPTS)
def test_does_not_call_dirname(script: Path, tool: str, skip_line: str) -> None:
    code = [ln for ln in script.read_text().splitlines() if not ln.lstrip().startswith("#")]
    assert not [ln for ln in code if "dirname" in ln], code
