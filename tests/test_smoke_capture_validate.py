"""
End-to-end smoke test: nova capture -> disk capsule -> nova validate.

No mocking. All real subsystems: capture orchestrator, capsule writer,
lineage writer, env capture, trace writer, schema validator.

If this test fails, something in the golden path is broken.
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml
from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()


class TestSmokeCaptureValidate:
    def test_capture_exit_zero(self, tmp_path: Path) -> None:
        runs_dir = tmp_path / "runs"
        result = runner.invoke(
            app,
            ["capture", "--output-dir", str(runs_dir), sys.executable, "-c", "print('smoke')"],
        )
        assert result.exit_code == 0, f"nova capture exited {result.exit_code}:\n{result.output}"

    def test_capsule_dir_created(self, tmp_path: Path) -> None:
        runs_dir = tmp_path / "runs"
        runner.invoke(
            app,
            ["capture", "--output-dir", str(runs_dir), sys.executable, "-c", "pass"],
        )
        run_dirs = [d for d in runs_dir.iterdir() if d.is_dir()]
        assert len(run_dirs) == 1, f"Expected 1 capsule dir, found: {run_dirs}"

    def test_capsule_yaml_exists_and_has_run_id(self, tmp_path: Path) -> None:
        runs_dir = tmp_path / "runs"
        runner.invoke(
            app,
            ["capture", "--output-dir", str(runs_dir), sys.executable, "-c", "pass"],
        )
        capsule_dir = next(runs_dir.iterdir())
        capsule_yaml = capsule_dir / "capsule.yaml"
        assert capsule_yaml.exists(), "capsule.yaml not found"
        manifest = yaml.safe_load(capsule_yaml.read_text())
        assert manifest.get("run_id"), f"run_id missing in manifest: {manifest}"

    def test_capsule_yaml_status_success(self, tmp_path: Path) -> None:
        runs_dir = tmp_path / "runs"
        runner.invoke(
            app,
            ["capture", "--output-dir", str(runs_dir), sys.executable, "-c", "pass"],
        )
        capsule_dir = next(runs_dir.iterdir())
        manifest = yaml.safe_load((capsule_dir / "capsule.yaml").read_text())
        assert manifest.get("exit_code") == 0, f"exit_code: {manifest.get('exit_code')}"
        assert manifest.get("status") == "success", f"status: {manifest.get('status')}"

    def test_env_lock_exists(self, tmp_path: Path) -> None:
        runs_dir = tmp_path / "runs"
        runner.invoke(
            app,
            ["capture", "--output-dir", str(runs_dir), sys.executable, "-c", "pass"],
        )
        capsule_dir = next(runs_dir.iterdir())
        assert (capsule_dir / "env.lock").exists(), "env.lock not found in capsule"

    def test_trace_jsonl_exists_and_nonempty(self, tmp_path: Path) -> None:
        runs_dir = tmp_path / "runs"
        runner.invoke(
            app,
            ["capture", "--output-dir", str(runs_dir), sys.executable, "-c", "pass"],
        )
        capsule_dir = next(runs_dir.iterdir())
        trace = capsule_dir / "trace.jsonl"
        assert trace.exists(), "trace.jsonl not found in capsule"
        assert trace.stat().st_size > 0, "trace.jsonl is empty"

    def test_validate_exits_zero(self, tmp_path: Path) -> None:
        runs_dir = tmp_path / "runs"
        runner.invoke(
            app,
            ["capture", "--output-dir", str(runs_dir), sys.executable, "-c", "pass"],
        )
        capsule_dir = next(runs_dir.iterdir())
        result = runner.invoke(app, ["validate", str(capsule_dir)])
        assert result.exit_code == 0, (
            f"nova validate exited {result.exit_code}:\n{result.output}"
        )

    def test_failure_run_captured_as_failure(self, tmp_path: Path) -> None:
        runs_dir = tmp_path / "runs"
        result = runner.invoke(
            app,
            ["capture", "--output-dir", str(runs_dir),
             sys.executable, "-c", "import sys; sys.exit(42)"],
        )
        assert result.exit_code == 42
        capsule_dir = next(runs_dir.iterdir())
        manifest = yaml.safe_load((capsule_dir / "capsule.yaml").read_text())
        assert manifest.get("exit_code") == 42
        assert manifest.get("status") == "failure"
