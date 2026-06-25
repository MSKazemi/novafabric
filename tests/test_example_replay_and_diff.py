"""End-to-end regression test for examples/replay-and-diff.

Mirrors what the README walks a user through: capture twice with
different AGENT_MODE values, then diff. If the example breaks
(schema change, CLI rename, etc.), this test fails loudly.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from typer.testing import CliRunner

from novafabric.cli.main import app

EXAMPLE = (
    Path(__file__).resolve().parent.parent
    / "examples" / "replay-and-diff" / "agent.py"
)


def _capture(runs_dir: Path, mode: str) -> Path:
    runner = CliRunner()
    env = os.environ.copy()
    env["AGENT_MODE"] = mode
    # CliRunner's invoke() does not pass env to the subprocess that
    # `nova capture` spawns by default — we set os.environ instead.
    saved = os.environ.get("AGENT_MODE")
    os.environ["AGENT_MODE"] = mode
    try:
        result = runner.invoke(
            app,
            ["capture", "--output-dir", str(runs_dir),
             sys.executable, str(EXAMPLE)],
        )
    finally:
        if saved is None:
            os.environ.pop("AGENT_MODE", None)
        else:
            os.environ["AGENT_MODE"] = saved
    assert result.exit_code == 0, (
        f"capture (mode={mode}) failed: exit={result.exit_code}\n{result.output}"
    )
    capsules = sorted(d for d in runs_dir.iterdir() if d.is_dir())
    return capsules[-1]


def test_example_file_exists() -> None:
    assert EXAMPLE.is_file(), f"example missing: {EXAMPLE}"


def test_capture_baseline_and_regressed_then_diff(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    cap_a = _capture(runs_dir, "baseline")
    cap_b = _capture(runs_dir, "regressed")

    assert cap_a != cap_b
    assert (cap_a / "capsule.yaml").exists()
    assert (cap_b / "capsule.yaml").exists()

    runner = CliRunner()
    result = runner.invoke(app, ["diff", str(cap_a), str(cap_b)])
    # diff must succeed (exit 0) even when differences are present;
    # exit 1 is reserved for --assert-no-regressions mode.
    assert result.exit_code == 0, (
        f"nova diff failed: exit={result.exit_code}\n{result.output}"
    )


def test_diff_assert_no_regressions_fails_on_changes(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    cap_a = _capture(runs_dir, "baseline")
    cap_b = _capture(runs_dir, "regressed")
    runner = CliRunner()
    result = runner.invoke(
        app, ["diff", str(cap_a), str(cap_b), "--assert-no-regressions"]
    )
    # If diff detects any change, --assert-no-regressions should exit 1.
    # If the engine reports no changes between these two runs, the example
    # is no longer demonstrating its point — fail the test so we notice.
    assert result.exit_code == 1, (
        f"expected exit 1 with --assert-no-regressions on differing capsules, "
        f"got exit={result.exit_code}\n{result.output}"
    )
