from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()

_MAKE_ADAPTER = "novafabric.object_capsule_store.backend_router.make_adapter"
_REBUILD_FN = "novafabric.object_capsule_store.rebuild.rebuild_metadata_db"


def _make_report(
    capsules_found: int = 3,
    elapsed: float = 0.12,
    warnings: list[str] | None = None,
) -> MagicMock:
    report = MagicMock()
    report.capsules_found = capsules_found
    report.time_to_replay_seconds = elapsed
    report.integrity_warnings = warnings or []
    return report


def test_rebuild_help() -> None:
    result = runner.invoke(app, ["rebuild-metadata-db", "--help"])
    assert result.exit_code == 0
    assert "prefix" in result.output or "rebuild" in result.output.lower()


def test_rebuild_success_no_warnings(tmp_path: Path) -> None:
    report = _make_report(capsules_found=5, elapsed=0.05)
    with (
        patch(_MAKE_ADAPTER, return_value=MagicMock()),
        patch(_REBUILD_FN, return_value=report),
    ):
        result = runner.invoke(
            app,
            ["rebuild-metadata-db", "--target-db", str(tmp_path / "out.db")],
        )
    assert result.exit_code == 0
    assert "5" in result.output
    assert "no integrity warnings" in result.output


def test_rebuild_with_integrity_warnings(tmp_path: Path) -> None:
    warnings = [f"hash mismatch on cap-{i:03d}" for i in range(3)]
    report = _make_report(capsules_found=3, elapsed=0.3, warnings=warnings)
    with (
        patch(_MAKE_ADAPTER, return_value=MagicMock()),
        patch(_REBUILD_FN, return_value=report),
    ):
        result = runner.invoke(app, ["rebuild-metadata-db"])
    assert result.exit_code == 0
    assert "warnings" in result.output
    assert "hash mismatch" in result.output


def test_rebuild_many_warnings_truncated() -> None:
    warnings = [f"warn-{i}" for i in range(25)]
    report = _make_report(warnings=warnings)
    with (
        patch(_MAKE_ADAPTER, return_value=MagicMock()),
        patch(_REBUILD_FN, return_value=report),
    ):
        result = runner.invoke(app, ["rebuild-metadata-db"])
    assert result.exit_code == 0
    assert "and 5 more" in result.output


def test_rebuild_backend_error_exits_1() -> None:
    with patch(_MAKE_ADAPTER, side_effect=ValueError("unknown backend")):
        result = runner.invoke(app, ["rebuild-metadata-db", "--backend", "bad"])
    assert result.exit_code != 0


def test_rebuild_with_prefix_and_data_dir(tmp_path: Path) -> None:
    report = _make_report()
    with (
        patch(_MAKE_ADAPTER, return_value=MagicMock()) as mock_adapter,
        patch(_REBUILD_FN, return_value=report),
    ):
        result = runner.invoke(
            app,
            [
                "rebuild-metadata-db",
                "--prefix", "tenant-a/",
                "--data-dir", str(tmp_path),
                "--backend", "local",
            ],
        )
    assert result.exit_code == 0
    call_kwargs = mock_adapter.call_args[1]
    assert "data_dir" in call_kwargs
