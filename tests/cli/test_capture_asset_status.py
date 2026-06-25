"""Tests for C-1.5 — --require-asset-status and --warn-if-asset-status flags.

Covers:
- check_asset_status() success (status in required set)
- check_asset_status() raises AssetStatusCheckError when status not in required set
- check_asset_status() does NOT raise for warn_statuses (only logs)
- check_asset_status() raises when asset not registered + require_registered=True
- check_asset_status() warns (no raise) when asset not registered + require_registered=False
- CaptureOrchestrator.run() is blocked before writing any file when check fails
- CLI --require-asset-status blocks capture and exits 1
- CLI --warn-if-asset-status does NOT block capture
- CLI with no --asset flag: capture proceeds normally (no registry lookup)
- --require-registered blocks capture when asset absent
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from novafabric.capture.orchestrator import AssetStatusCheckError, check_asset_status
from novafabric.cli.main import app
from novafabric.registry.service import (
    promote_asset,
    register_asset,
)
from novafabric.spec.models import AssetStatus
from novafabric.spec.validator import validate_spec

runner = CliRunner()

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

_TOOL_STAGING_YAML = """\
novafabric_spec_version: "1"
asset_type: tool
name: cap-check-tool
version: 1.0.0
status: development
description: tool for capture check tests
spec:
  entrypoint: /run
"""


def _write_spec(tmp_path: Path, content: str, filename: str) -> Path:
    p = tmp_path / filename
    p.write_text(content)
    return p


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "test_reg.db"


@pytest.fixture
def staging_asset(tmp_path: Path, db_path: Path) -> str:
    """Register cap-check-tool@1.0.0 and promote to staging; return the asset_ref."""
    p = _write_spec(tmp_path, _TOOL_STAGING_YAML, "tool.yaml")
    spec = validate_spec(p)
    register_asset(spec, p, db_path=db_path)
    promote_asset("cap-check-tool", "1.0.0", AssetStatus.staging, "test-user", db_path=db_path)
    return "cap-check-tool@1.0.0"


# ---------------------------------------------------------------------------
# check_asset_status() unit tests
# ---------------------------------------------------------------------------


class TestCheckAssetStatus:
    def test_status_in_required_set_passes(
        self, tmp_path: Path, db_path: Path, staging_asset: str
    ) -> None:
        """No exception when asset status is in the required set."""
        check_asset_status(
            asset_ref=staging_asset,
            require_statuses=["staging", "production"],
            warn_statuses=None,
            require_registered=False,
            db_path=db_path,
        )

    def test_status_not_in_required_set_raises(
        self, tmp_path: Path, db_path: Path, staging_asset: str
    ) -> None:
        """AssetStatusCheckError when asset status is not in required set."""
        with pytest.raises(AssetStatusCheckError, match="not in the required set"):
            check_asset_status(
                asset_ref=staging_asset,
                require_statuses=["production"],
                warn_statuses=None,
                require_registered=False,
                db_path=db_path,
            )

    def test_warn_statuses_do_not_raise(
        self, tmp_path: Path, db_path: Path, staging_asset: str
    ) -> None:
        """warn_statuses matching the asset's status does NOT raise (only logs)."""
        # Must not raise even if asset is in the warn set
        check_asset_status(
            asset_ref=staging_asset,
            require_statuses=None,
            warn_statuses=["staging"],
            require_registered=False,
            db_path=db_path,
        )

    def test_unregistered_asset_with_require_registered_raises(
        self, tmp_path: Path, db_path: Path
    ) -> None:
        """require_registered=True blocks capture for unknown asset."""
        with pytest.raises(AssetStatusCheckError, match="not registered"):
            check_asset_status(
                asset_ref="ghost-agent@v1",
                require_statuses=None,
                warn_statuses=None,
                require_registered=True,
                db_path=db_path,
            )

    def test_unregistered_asset_without_require_registered_does_not_raise(
        self, tmp_path: Path, db_path: Path
    ) -> None:
        """Default behaviour: unregistered asset warns but does not block."""
        # Must not raise
        check_asset_status(
            asset_ref="ghost-agent@v1",
            require_statuses=None,
            warn_statuses=None,
            require_registered=False,
            db_path=db_path,
        )

    def test_bare_name_without_version_resolves(
        self, tmp_path: Path, db_path: Path, staging_asset: str
    ) -> None:
        """check_asset_status accepts bare name without version (resolves latest)."""
        check_asset_status(
            asset_ref="cap-check-tool",
            require_statuses=["staging"],
            warn_statuses=None,
            require_registered=False,
            db_path=db_path,
        )


# ---------------------------------------------------------------------------
# Orchestrator integration tests (no actual subprocess needed for status check)
# ---------------------------------------------------------------------------


class TestOrchestratorAssetStatusCheck:
    def test_orchestrator_blocks_before_writing_files(
        self, tmp_path: Path, db_path: Path, staging_asset: str
    ) -> None:
        """When require check fails, no capsule directory is created."""
        from novafabric.capture.orchestrator import CaptureOrchestrator

        runs_dir = tmp_path / "runs"
        orch = CaptureOrchestrator(base_dir=runs_dir)

        with pytest.raises(AssetStatusCheckError):
            orch.run(
                command=[sys.executable, "-c", "pass"],
                asset_ref=staging_asset,
                require_asset_statuses=["production"],  # staging → fails
                registry_db_path=db_path,
            )

        # No capsule files should have been written
        if runs_dir.exists():
            assert list(runs_dir.iterdir()) == [], (
                "Expected no capsule directories after a blocked capture"
            )

    def test_orchestrator_allows_when_status_matches(
        self, tmp_path: Path, db_path: Path, staging_asset: str
    ) -> None:
        """Capture proceeds when status is in required set."""
        from novafabric.capture.orchestrator import CaptureOrchestrator

        runs_dir = tmp_path / "runs"
        orch = CaptureOrchestrator(base_dir=runs_dir)

        result = orch.run(
            command=[sys.executable, "-c", "pass"],
            asset_ref=staging_asset,
            require_asset_statuses=["staging", "production"],
            registry_db_path=db_path,
        )
        assert result.exit_code == 0

    def test_orchestrator_no_asset_ref_skips_check(
        self, tmp_path: Path, db_path: Path
    ) -> None:
        """Capture without --asset skips status check entirely."""
        from novafabric.capture.orchestrator import CaptureOrchestrator

        runs_dir = tmp_path / "runs"
        orch = CaptureOrchestrator(base_dir=runs_dir)

        result = orch.run(
            command=[sys.executable, "-c", "pass"],
            asset_ref=None,
            require_asset_statuses=["production"],  # would block if checked
            registry_db_path=db_path,
        )
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# CLI integration tests
# ---------------------------------------------------------------------------


class TestCaptureCLI:
    def test_require_asset_status_blocks_capture(
        self, tmp_path: Path, db_path: Path, staging_asset: str,
        monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--require-asset-status exits 1 when status doesn't match."""
        monkeypatch.setenv("NOVAFABRIC_DB_PATH", str(db_path))
        result = runner.invoke(
            app,
            [
                "capture",
                "--output-dir", str(tmp_path / "runs"),
                "--asset", "cap-check-tool@1.0.0",
                "--require-asset-status", "production",
                sys.executable, "-c", "pass",
            ],
        )
        assert result.exit_code == 1
        assert "Asset status check failed" in result.output

    def test_require_asset_status_allows_when_matching(
        self, tmp_path: Path, db_path: Path, staging_asset: str,
        monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--require-asset-status passes when status matches."""
        monkeypatch.setenv("NOVAFABRIC_DB_PATH", str(db_path))
        result = runner.invoke(
            app,
            [
                "capture",
                "--output-dir", str(tmp_path / "runs"),
                "--asset", "cap-check-tool@1.0.0",
                "--require-asset-status", "staging,production",
                sys.executable, "-c", "pass",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "Capsule written" in result.output

    def test_warn_if_asset_status_does_not_block(
        self, tmp_path: Path, db_path: Path, staging_asset: str,
        monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--warn-if-asset-status does not block capture; capsule is written."""
        monkeypatch.setenv("NOVAFABRIC_DB_PATH", str(db_path))
        result = runner.invoke(
            app,
            [
                "capture",
                "--output-dir", str(tmp_path / "runs"),
                "--asset", "cap-check-tool@1.0.0",
                "--warn-if-asset-status", "staging",
                sys.executable, "-c", "pass",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "Capsule written" in result.output

    def test_no_asset_flag_skips_check(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without --asset, capture proceeds without any registry lookup."""
        result = runner.invoke(
            app,
            [
                "capture",
                "--output-dir", str(tmp_path / "runs"),
                sys.executable, "-c", "pass",
            ],
        )
        assert result.exit_code == 0, result.output

    def test_require_registered_blocks_unregistered_asset(
        self, tmp_path: Path, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--require-registered blocks capture when asset is not in the registry."""
        monkeypatch.setenv("NOVAFABRIC_DB_PATH", str(db_path))
        result = runner.invoke(
            app,
            [
                "capture",
                "--output-dir", str(tmp_path / "runs"),
                "--asset", "ghost-agent@v1",
                "--require-registered",
                sys.executable, "-c", "pass",
            ],
        )
        assert result.exit_code == 1
        assert "Asset status check failed" in result.output

    def test_unregistered_asset_without_require_registered_is_allowed(
        self, tmp_path: Path, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Unregistered asset without --require-registered produces a warning, not a block."""
        monkeypatch.setenv("NOVAFABRIC_DB_PATH", str(db_path))
        result = runner.invoke(
            app,
            [
                "capture",
                "--output-dir", str(tmp_path / "runs"),
                "--asset", "ghost-agent@v1",
                # no --require-registered
                sys.executable, "-c", "pass",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "Capsule written" in result.output
