"""Every example capsule passes ``nova validate`` as git records it, not as the working tree has it.

``examples/capsules/minimal-run`` failed ``nova validate`` (exit 1, ``missing directory:
inputs``) in every fresh checkout: a capsule's ``inputs/`` is often empty, and git does not
record an empty directory. The validator requires the directory, so the example needs a
tracked placeholder in it.

Same lesson as ``test_example_scripts_are_executable_in_git.py``: a property that must
survive a clone is asserted against the git index, never against the working tree.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from novafabric.cli.main import app

REPO_ROOT = Path(__file__).resolve().parents[2]
_EXAMPLES = "examples/capsules"


def _tracked(*args: str) -> list[str]:
    proc = subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        pytest.skip("not a git checkout (sdist or exported tree)")
    return [line for line in proc.stdout.splitlines() if line]


def _example_capsules() -> list[str]:
    files = _tracked("ls-files", "--", _EXAMPLES)
    return sorted({str(Path(f).parent) for f in files if Path(f).name == "capsule.yaml"})


def test_there_is_at_least_one_example_capsule() -> None:
    assert _example_capsules(), f"no capsule.yaml tracked under {_EXAMPLES}"


@pytest.mark.parametrize("capsule", _example_capsules())
def test_example_capsule_validates_from_a_fresh_checkout(capsule: str, tmp_path: Path) -> None:
    files = _tracked("ls-files", "--", capsule)
    prefix = f"{tmp_path}/"
    _tracked("checkout-index", f"--prefix={prefix}", "--", *files)
    result = CliRunner().invoke(app, ["validate", str(tmp_path / capsule)])
    assert result.exit_code == 0, result.output
