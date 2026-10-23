"""L19: the capture GitHub Action uploads an invalid capsule, then fails last.

``.github/actions/capture/action.yml`` ran ``nova validate`` with no guard. A composite
action abandons its remaining steps once one fails (``always()`` does not rescue them),
so an invalid capsule from a crashing run was never uploaded, and the job failed with
``nova validate``'s exit code instead of the command's own.

These tests execute the real ``run:`` bodies of the validate and propagate steps with
bash, substituting the ``${{ … }}`` expressions and putting a fake ``nova`` on PATH. No
Docker, no network, no GitHub.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

ACTION = Path(__file__).resolve().parents[1] / ".github" / "actions" / "capture" / "action.yml"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


def _action() -> dict[str, Any]:
    return yaml.safe_load(ACTION.read_text())


def _step(name: str) -> dict[str, Any]:
    for step in _action()["runs"]["steps"]:
        if step.get("name") == name:
            return step
    raise AssertionError(f"no step named {name!r}")


def _step_names() -> list[str]:
    return [s.get("name", "") for s in _action()["runs"]["steps"]]


def _run_step(
    body: str, tmp_path: Path, exprs: dict[str, str], nova_rc: int = 0
) -> tuple[subprocess.CompletedProcess[str], dict[str, str], str]:
    for expr, value in exprs.items():
        body = body.replace("${{ " + expr + " }}", value)
    assert "${{" not in body, f"unsubstituted expression in step body:\n{body}"
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    shim = bindir / "nova"
    shim.write_text(f"#!/usr/bin/env bash\necho \"nova $*\"\nexit {nova_rc}\n")
    shim.chmod(0o755)
    out, env_file, summary = (tmp_path / n for n in ("output", "env", "summary"))
    for f in (out, env_file, summary):
        f.write_text("")
    script = tmp_path / "step.sh"
    script.write_text(body)
    env = {
        "PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}",
        "GITHUB_OUTPUT": str(out),
        "GITHUB_ENV": str(env_file),
        "GITHUB_STEP_SUMMARY": str(summary),
        "HOME": str(tmp_path),
    }
    # GitHub runs composite `shell: bash` steps as `bash --noprofile --norc -eo pipefail`.
    proc = subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", str(script)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    kv: dict[str, str] = {}
    for line in (out.read_text() + env_file.read_text()).splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            kv[k] = v
    return proc, kv, summary.read_text()


CAPTURE_EXPRS = {
    "steps.capture.outputs.capsule-path": "/tmp/caps/01HRUN",
    "steps.capture.outputs.run-id": "01HRUN",
    "inputs.artifact-name": "novafabric-capsule",
    "inputs.validate": "true",
}


def test_step_order_validate_then_upload_then_propagate() -> None:
    names = _step_names()
    assert names.index("Validate the capsule") < names.index("Upload the capsule")
    assert names.index("Upload the capsule") < len(names) - 1
    assert names[-1] == "Propagate the captured command's exit code"


def test_validate_step_never_fails_and_records_valid_false(tmp_path: Path) -> None:
    step = _step("Validate the capsule")
    assert step.get("id") == "validate"
    proc, kv, summary = _run_step(step["run"], tmp_path, CAPTURE_EXPRS, nova_rc=1)
    assert proc.returncode == 0, proc.stderr
    assert kv["valid"] == "false"
    assert kv["validate-exit-code"] == "1"
    assert kv["NOVAFABRIC_VALID"] == "false"
    assert "Valid" in summary


def test_validate_step_records_valid_true(tmp_path: Path) -> None:
    proc, kv, _ = _run_step(_step("Validate the capsule")["run"], tmp_path, CAPTURE_EXPRS)
    assert proc.returncode == 0, proc.stderr
    assert kv["valid"] == "true"
    assert kv["NOVAFABRIC_VALID"] == "true"


def _propagate(tmp_path: Path, exit_code: str, valid: str) -> subprocess.CompletedProcess[str]:
    exprs = {
        **CAPTURE_EXPRS,
        "steps.capture.outputs.exit-code": exit_code,
        "steps.validate.outputs.valid": valid,
        "steps.validate.outputs.validate-exit-code": "1" if valid == "false" else "0",
        "inputs.upload-artifact": "true",
    }
    proc, _, _ = _run_step(
        _step("Propagate the captured command's exit code")["run"], tmp_path, exprs
    )
    return proc


def test_propagate_fails_on_invalid_capsule_after_upload(tmp_path: Path) -> None:
    proc = _propagate(tmp_path, exit_code="0", valid="false")
    assert proc.returncode == 1
    assert "::error title=Invalid capsule::" in proc.stdout
    assert "01HRUN" in proc.stdout
    assert "novafabric-capsule" in proc.stdout


def test_propagate_prefers_command_exit_code(tmp_path: Path) -> None:
    proc = _propagate(tmp_path, exit_code="3", valid="false")
    assert proc.returncode == 3
    assert "Captured command failed" in proc.stdout
    assert "nova validate rejected" in proc.stdout


def test_valid_capsule_path_unchanged(tmp_path: Path) -> None:
    proc = _propagate(tmp_path, exit_code="0", valid="true")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "::error" not in proc.stdout


def test_validation_skipped_never_fails(tmp_path: Path) -> None:
    """``validate: false``: the validate step never ran, so its output is empty."""
    proc = _propagate(tmp_path, exit_code="0", valid="")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    proc = _propagate(tmp_path, exit_code="2", valid="")
    assert proc.returncode == 2


def test_valid_output_is_declared() -> None:
    outputs = _action()["outputs"]
    assert outputs["valid"]["value"] == "${{ steps.validate.outputs.valid }}"


def test_validate_input_description_promises_the_upload() -> None:
    desc = _action()["inputs"]["validate"]["description"]
    assert "still uploaded" in desc
