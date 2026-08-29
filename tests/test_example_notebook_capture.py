"""Regression tests for examples/notebook-capture.

The example makes three claims, and each is asserted here:

* the documented command captures a whole notebook execution as one capsule;
* with no Jupyter on PATH the script exits **0** with a skip message, because
  `nbconvert` is an extra of the example and never a NovaFabric dependency;
* the notebook is deterministic and standard-library only, so it needs no API
  key and no network.

The capture test is skip-guarded on `jupyter` being on PATH. That guard is
honest rather than convenient: NovaFabric ships no notebook code at all, so
there is nothing to stand in for a real kernel, and a test that faked one would
assert against the one shape that cannot expose the example's real failure mode
(a kernel living in a different environment, where capture hooks silently fail).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

EXAMPLE_DIR = Path(__file__).resolve().parent.parent / "examples" / "notebook-capture"
NOTEBOOK = EXAMPLE_DIR / "analysis.ipynb"
RUN_SH = EXAMPLE_DIR / "run.sh"


def _notebook() -> dict:
    return json.loads(NOTEBOOK.read_text())


def _code_cells() -> list[dict]:
    return [c for c in _notebook()["cells"] if c["cell_type"] == "code"]


def _sole_capsule(out_dir: Path) -> Path:
    """The one capsule under *out_dir*, chosen by `capsule.yaml`.

    Not "the only directory": nbconvert writes `executed.ipynb` beside it and
    the hermetic-env fixture leaves its own directory there too.
    """
    found = [d for d in out_dir.iterdir() if d.is_dir() and (d / "capsule.yaml").is_file()]
    assert len(found) == 1, found
    return found[0]


def test_the_example_files_exist_and_the_script_is_executable() -> None:
    assert NOTEBOOK.is_file()
    assert (EXAMPLE_DIR / "README.md").is_file()
    assert RUN_SH.is_file()
    assert RUN_SH.stat().st_mode & 0o111, "run.sh must be executable"


def test_the_notebook_is_valid_and_has_executable_cells() -> None:
    nb = _notebook()
    assert nb["nbformat"] == 4
    assert len(_code_cells()) >= 1


def test_every_cell_has_an_id() -> None:
    """nbformat >= 4.5 requires it; without ids nbconvert emits a deprecation
    warning into the captured stderr on every single run."""
    for cell in _notebook()["cells"]:
        assert cell.get("id"), cell


def test_the_notebook_is_stdlib_only() -> None:
    """No API key, no network, no third-party install — so it runs anywhere."""
    allowed = {"json", "os", "statistics", "pathlib"}
    imported: set[str] = set()
    for cell in _code_cells():
        for line in "".join(cell["source"]).splitlines():
            line = line.strip()
            if line.startswith("import "):
                imported.add(line.removeprefix("import ").split()[0].split(".")[0])
            elif line.startswith("from "):
                imported.add(line.removeprefix("from ").split()[0].split(".")[0])
    assert imported <= allowed, f"non-stdlib imports: {imported - allowed}"


def test_the_notebook_body_is_deterministic() -> None:
    """Two runs must be comparable, which rules out the usual suspects."""
    source = "".join("".join(c["source"]) for c in _code_cells())
    for banned in ("random.", "time.time(", "datetime.now(", "uuid4("):
        assert banned not in source, banned


def test_run_sh_skips_cleanly_when_jupyter_is_absent(tmp_path: Path) -> None:
    """The acceptance criterion that protects a clean checkout.

    `nbconvert` is an extra for this example only, so its absence must be a
    skip with exit 0 — never a failure.
    """
    # An empty PATH is the point: it must hide `jupyter`. Bash itself is
    # therefore invoked by absolute path, or the test would fail on finding
    # the interpreter rather than on the behaviour it means to check.
    bash = shutil.which("bash") or "/bin/bash"
    env = {"PATH": "/nonexistent", "HOME": str(tmp_path)}
    result = subprocess.run(
        [bash, str(RUN_SH), str(tmp_path / "capsules")],
        capture_output=True, text=True, env=env, timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert "skip:" in result.stdout
    assert "not a NovaFabric dependency" in result.stdout


@pytest.mark.skipif(shutil.which("jupyter") is None, reason="jupyter is not on PATH")
def test_capture_of_the_notebook_produces_a_valid_capsule(tmp_path: Path) -> None:
    """Pattern 1 from the README, end to end against a real kernel."""
    out = tmp_path / "capsules"
    out.mkdir()
    # Same variable run.sh exports. Without it the notebook writes results.json
    # into the example's own directory and the test dirties the checkout.
    env = {**os.environ, "NOTEBOOK_OUTPUT_DIR": str(out)}
    result = subprocess.run(
        [sys.executable, "-m", "novafabric.cli.main", "capture",
         "--output-dir", str(out), "--",
         "jupyter", "nbconvert", "--to", "notebook", "--execute", str(NOTEBOOK),
         "--output-dir", str(out), "--output", "executed.ipynb"],
        capture_output=True, text=True, timeout=600, env=env,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    capsule = _sole_capsule(out)
    manifest = yaml.safe_load((capsule / "capsule.yaml").read_text())
    assert manifest["status"] == "success"
    assert manifest["exit_code"] == 0

    # The README's headline limitation: cell output never reaches the capsule.
    # If this ever starts failing, the README is what needs updating.
    printed = "notebook: wrote"
    in_capsule = any(
        printed in p.read_text(errors="ignore")
        for p in capsule.rglob("*") if p.is_file()
    )
    assert not in_capsule, (
        "cell stdout now reaches the capsule — README 'What is not captured' "
        "item 1 is stale and must be rewritten"
    )
