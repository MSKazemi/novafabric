"""Regression tests for examples/blackbox_demo.

The demo's step 1 used to grep ``.novafabric/runs/<id>`` out of ``nova
capture`` output, a path the CLI stopped writing long ago — every run died with
"Could not parse capsule path". These tests drive the demo's parsing against
*real* ``nova capture`` output (in a temporary NOVAFABRIC_HOME, never the
user's), and run the whole demo end to end, so the script cannot drift from the
CLI again.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

DEMO_DIR = Path(__file__).resolve().parent.parent / "examples" / "blackbox_demo"
HELPER = DEMO_DIR / "capsule_path.sh"
SCRIPT = DEMO_DIR / "run_demo.sh"
README = DEMO_DIR / "README.md"
BIN_DIR = Path(sys.executable).parent
NOVA = BIN_DIR / "nova"

pytestmark = pytest.mark.skipif(
    shutil.which("sh") is None or not NOVA.is_file(),
    reason="needs a POSIX sh and the installed nova console script",
)


def _isolated_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("NOVAFABRIC_") and k not in {"FORCE_COLOR", "COLORTERM"}
    }
    env["NOVAFABRIC_HOME"] = str(tmp_path / "home")
    env["NOVAFABRIC_SUGGEST"] = "0"
    env["PATH"] = f"{BIN_DIR}:{env.get('PATH', '')}"
    env.update(extra)
    return env


def _capsule_path(base: Path, output: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", "-c", '. "$0"; capsule_path "$1" "$2"', str(HELPER), str(base), output],
        capture_output=True,
        text=True,
        timeout=30,
    )


def _real_capture(tmp_path: Path, base: Path, columns: str) -> str:
    result = subprocess.run(
        [str(NOVA), "capture", "--output-dir", str(base), "--", sys.executable, "-c", "print(1)"],
        capture_output=True,
        text=True,
        env=_isolated_env(tmp_path, COLUMNS=columns),
        cwd=tmp_path,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout + result.stderr


def test_capsule_path_resolves_real_capture_output(tmp_path: Path) -> None:
    """At 40 columns the console breaks the printed path across lines; the
    run-id token survives, so the helper still finds the exact directory.
    (The default width is covered by test_demo_runs_end_to_end.)"""
    base = tmp_path / "a deep capsule base directory" / "with spaces"
    output = _real_capture(tmp_path, base, columns="40")

    result = _capsule_path(base, output)

    assert result.returncode == 0, result.stderr
    capsule = Path(result.stdout.strip())
    assert capsule.parent == base
    assert (capsule / "capsule.yaml").is_file()
    assert (capsule / "model-calls.jsonl").is_file()


def test_capsule_path_fails_when_output_has_no_run_id(tmp_path: Path) -> None:
    result = _capsule_path(tmp_path, "✗ Cannot start capture: something broke")
    assert result.returncode == 1
    assert result.stdout == ""


def test_capsule_path_fails_when_the_directory_does_not_exist(tmp_path: Path) -> None:
    result = _capsule_path(tmp_path, "✓ Capsule written: x  (run_id=01M4FCCTZX5M9K118VDKR9ZD17)")
    assert result.returncode == 1
    assert result.stdout == ""


def test_no_demo_surface_names_the_retired_runs_path() -> None:
    for path in (SCRIPT, README):
        assert ".novafabric/runs" not in path.read_text(encoding="utf-8"), path


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_demo_runs_end_to_end(tmp_path: Path) -> None:
    pytest.importorskip("openai")
    env = _isolated_env(
        tmp_path,
        NOVA=str(NOVA),
        PYTHON=sys.executable,
        DEMO_PORT=str(_free_port()),
        SKIP_VERIFY="1",
    )
    result = subprocess.run(
        ["sh", str(SCRIPT)],
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
        timeout=300,
    )
    log = result.stdout + result.stderr
    assert result.returncode == 0, log
    assert "DEMO COMPLETE" in result.stdout
    capsules = sorted((tmp_path / "home" / "capsules").iterdir())
    assert len(capsules) == 2, capsules
    for capsule in capsules:
        assert f"{capsule}" in result.stdout
