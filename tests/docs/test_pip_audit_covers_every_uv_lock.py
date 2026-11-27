"""Every tracked uv.lock must go through the pip-audit severity gate.

`test_dependabot_covers_every_manifest.py` makes sure every lockfile is
*watched* for updates. Being watched is not being *gated*: Dependabot proposes
a PR, nothing forces anyone to merge it, and a lockfile outside the pip-audit
job can ship a HIGH advisory while CI stays green. Until this guard existed,
`.github/workflows/pip-audit.yml` exported and audited only the root
`uv.lock`; `examples/plugin-hook-reference/uv.lock`, a file readers clone and
install, was never audited even though it pins its own versions of
novafabric's transitive dependencies.

The lockfile list comes from `git ls-files`, so adding a new uv-managed
sub-project fails here until the workflow audits it too.
"""

from __future__ import annotations

import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "pip-audit.yml"
JOB = "audit"


def _tracked_lock_dirs() -> list[str]:
    """Directories (repo-relative, root = ".") holding a uv.lock the public git tracks."""
    result = subprocess.run(
        ["git", "ls-files", "*uv.lock"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    dirs = {
        str(Path(line).parent)
        for line in result.stdout.splitlines()
        if Path(line).name == "uv.lock"
    }
    return sorted(dirs)


def _steps() -> list[dict[str, object]]:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"][JOB]["steps"]
    assert isinstance(steps, list)
    return steps


def _run_scripts() -> list[str]:
    return [str(step["run"]) for step in _steps() if "run" in step]


def _exports() -> dict[str, str]:
    """Map each `uv export` step's project directory to the file it writes."""
    exports: dict[str, str] = {}
    for script in _run_scripts():
        if "uv export" not in script:
            continue
        argv = shlex.split(script)
        project = argv[argv.index("--project") + 1] if "--project" in argv else "."
        output = argv[argv.index("-o") + 1]
        assert "--frozen" in argv, f"`uv export` for {project} must use --frozen: {script}"
        exports[str(Path(project))] = output
    return exports


def test_the_workflow_exports_at_least_the_root_lock() -> None:
    """Non-vacuity: the parser must find the root export, or every check below is hollow."""
    assert "." in _exports(), f"no root `uv export` step found in {WORKFLOW.name}"
    assert len(_tracked_lock_dirs()) >= 2, "expected the root and example-plugin uv.lock"


@pytest.mark.parametrize("lock_dir", _tracked_lock_dirs())
def test_every_tracked_uv_lock_is_audited_and_gated(lock_dir: str) -> None:
    """Each lockfile is exported, audited by pip-audit, and passed through the gate.

    If this fails, add the export + audit + gate steps to pip-audit.yml; do not
    narrow the test. An ungated lockfile can carry a HIGH advisory with CI green.
    """
    exports = _exports()
    assert lock_dir in exports, (
        f"{lock_dir}/uv.lock is tracked but no `uv export` step in {WORKFLOW.name} "
        f"exports it (use `--project {lock_dir}`)"
    )
    requirements = exports[lock_dir]

    audit = [s for s in _run_scripts() if "pip-audit -r" in s and f"-r {requirements}" in s]
    assert audit, f"no pip-audit step reads {requirements} (exported from {lock_dir})"
    match = re.search(r"--output\s+(\S+)", audit[0])
    assert match, f"the pip-audit step for {requirements} writes no --output report"
    report = match.group(1)

    gate = [s for s in _run_scripts() if "pip_audit_gate.py" in s and f"--report {report}" in s]
    assert gate, f"no pip_audit_gate.py step reads {report}; findings in {lock_dir} never block"
    assert all("--waivers .pip-audit-waivers.toml" in s for s in gate), (
        "every gate run must use the one checked-in waiver file"
    )


def test_a_lock_missing_from_the_workflow_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    """Failure case: a lock directory with no export step fails with a useful message."""
    monkeypatch.setattr(sys.modules[__name__], "_exports", lambda: {".": "r.txt"})
    with pytest.raises(AssertionError, match="no `uv export` step"):
        test_every_tracked_uv_lock_is_audited_and_gated("examples/not-audited")
