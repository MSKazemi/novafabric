"""LSF and PBS job scripts quote what they interpolate (review capture-runners-23).

``export NOVAFABRIC_CAPSULE_DIR={capsule_dir}`` with a path holding a space exported
a truncated value (and ``$(...)`` in it ran), so the hooks wrote to the wrong place or
did not load and capture was silently empty. SLURM already ``shlex.quote``s every
value. Directive lines (``#BSUB``/``#PBS``) cannot be shell-quoted: a value with a
newline would inject a directive, and one with whitespace is cut; both are refused.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from novafabric.runners import LSFRunner, PBSRunner, RunnerJobSpec


def _spec(capsule: Path, **options: object) -> RunnerJobSpec:
    capsule.mkdir(parents=True, exist_ok=True)
    return RunnerJobSpec(
        run_id="01TESTQUOTE000000000000000", command=["python", "agent.py"],
        capsule_dir=capsule, env={"PATH": "/usr/bin"}, timeout_s=5.0,
        runner_options={"poll_interval_s": 0.01, **options},
    )


def _lsf_script(spec: RunnerJobSpec) -> tuple[str, Any]:
    seen: dict[str, str] = {}

    def fake(argv: list[str], *_a: Any, **k: Any) -> subprocess.CompletedProcess[bytes]:
        if Path(str(argv[0])).name == "bsub":
            seen["script"] = k["input"].decode()
            return subprocess.CompletedProcess(argv, 0, b"Job <7> is submitted.\n", b"")
        return subprocess.CompletedProcess(argv, 0, b"7 u DONE q h h j Oct 24\n", b"")

    with patch("subprocess.run", side_effect=fake), patch("time.sleep"):
        result = LSFRunner().run(spec)
    return seen.get("script", ""), result


def _pbs_script(spec: RunnerJobSpec) -> tuple[str, Any]:
    seen: dict[str, str] = {}

    def fake(argv: list[str], *_a: Any, **_k: Any) -> subprocess.CompletedProcess[bytes]:
        if Path(str(argv[0])).name == "qsub":
            seen["script"] = Path(str(argv[-1])).read_text()
            return subprocess.CompletedProcess(argv, 0, b"8.server\n", b"")
        return subprocess.CompletedProcess(argv, 0, b"job_state = F\nExit_status = 0\n", b"")

    with patch("subprocess.run", side_effect=fake), patch("time.sleep"):
        result = PBSRunner().run(spec)
    return seen.get("script", ""), result


def _exported(script: str, name: str, cwd: Path) -> str:
    """What bash makes of the script's ``export <name>=...`` line (run inside *cwd*)."""
    (line,) = [ln for ln in script.splitlines() if ln.startswith(f"export {name}=")]
    done = subprocess.run(
        ["bash", "-c", f'{line}\nprintf %s "${name}"'], capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": "/existing"}, timeout=10, cwd=cwd,
    )
    assert done.returncode == 0, done.stderr
    return done.stdout


@pytest.mark.parametrize("build", [_lsf_script, _pbs_script], ids=["lsf", "pbs"])
def test_a_capsule_path_with_a_space_and_a_dollar_is_exported_verbatim(
    tmp_path: Path, build: Any
) -> None:
    capsule = tmp_path / "My Project $(touch pwned)" / "cap"
    logs = tmp_path / "logs"
    script, result = build(_spec(capsule, output_dir=str(logs)))
    assert result.runner_status != "failed_setup", result.runner_error
    assert _exported(script, "NOVAFABRIC_CAPSULE_DIR", tmp_path) == str(capsule)
    assert _exported(script, "PYTHONPATH", tmp_path) == f"{capsule}:/existing"
    assert not (tmp_path / "pwned").exists()  # the $(...) in the path never ran


@pytest.mark.parametrize("build", [_lsf_script, _pbs_script], ids=["lsf", "pbs"])
@pytest.mark.parametrize("option,value", [
    ("job_name", "nova\n#BSUB -q evil"),
    ("queue", "normal extra"),
    ("output_dir", "/scratch/My Logs"),
])
def test_a_directive_value_that_cannot_be_carried_is_refused(
    tmp_path: Path, build: Any, option: str, value: str
) -> None:
    script, result = build(_spec(tmp_path / "cap", **{option: value}))
    assert result.runner_status == "failed_setup"
    assert option in (result.runner_error or "")
    assert script == ""  # nothing was submitted


def test_an_lsf_resource_requirement_keeps_its_spaces(tmp_path: Path) -> None:
    script, result = _lsf_script(_spec(tmp_path / "cap", resource_requirement="select[mem>4000] rusage[mem=4000]"))
    assert '#BSUB -R "select[mem>4000] rusage[mem=4000]"' in script
