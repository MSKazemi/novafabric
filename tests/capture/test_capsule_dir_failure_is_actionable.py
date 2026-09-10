"""A full or unwritable capsule directory is an operational condition, not a bug.

Defect **B11**. `nova capture -o <unwritable>` died with an **83-line Rich
traceback** ending in `OSError: [Errno 28] No space left on device`, before the
workload ran. The errno was the only information in it: not which directory
NovaFabric chose, not that it was NovaFabric's choice rather than the workload's,
and not what to do next.

A full scratch filesystem mid-campaign is the ordinary case on the machines this
runs on, so this is a routine failure that must read like one. Same class as B4
(`nova db upgrade` raw traceback when the `server` extra is absent), which was
fixed by naming the remedy — this follows that precedent.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from novafabric.capture.orchestrator import CapsuleDirectoryError, CaptureOrchestrator
from novafabric.cli.main import app

runner = CliRunner()


class TestOrchestratorRaisesANamedError:
    def test_missing_parent_raises_capsule_directory_error(
        self, tmp_path: Path
    ) -> None:
        target = Path("/proc/nonexistent/caps")  # /proc rejects mkdir
        with pytest.raises(CapsuleDirectoryError) as excinfo:
            CaptureOrchestrator(base_dir=target)
        message = str(excinfo.value)
        assert str(target) in message, "the message must name the directory"
        assert "workload was not started" in message, (
            "the operator needs to know nothing is half-captured"
        )
        assert "-o" in message, "the message must name the way out"

    def test_a_full_disk_is_reported_not_raised_raw(self, tmp_path: Path) -> None:
        """ENOSPC is the condition actually reported in B11."""
        enospc = OSError(28, "No space left on device")
        enospc.errno = 28
        with patch.object(Path, "mkdir", side_effect=enospc):
            with pytest.raises(CapsuleDirectoryError) as excinfo:
                CaptureOrchestrator(base_dir=tmp_path / "caps")
        message = str(excinfo.value)
        assert "No space left on device" in message
        assert "errno 28" in message
        # Chained, so the original OSError is still available to a caller that
        # wants it -- the point is the message, not hiding the cause.
        assert isinstance(excinfo.value.__cause__, OSError)

    def test_the_error_is_not_a_bare_oserror(self, tmp_path: Path) -> None:
        """A caller must be able to catch *this* condition specifically, rather
        than catching OSError and guessing which of a dozen causes it was."""
        assert not issubclass(CapsuleDirectoryError, OSError)

    def test_a_writable_directory_still_works(self, tmp_path: Path) -> None:
        """Guard the guard: if construction failed for everything, the tests
        above would pass while capture was entirely broken."""
        orch = CaptureOrchestrator(base_dir=tmp_path / "fresh" / "nested")
        assert orch is not None
        assert (tmp_path / "fresh" / "nested").is_dir()


class TestTheCLIPrintsOneActionableLine:
    def test_no_traceback_and_exit_1(self) -> None:
        result = runner.invoke(
            app, ["capture", "-o", "/proc/nonexistent/caps", "--", "true"]
        )
        assert result.exit_code == 1, result.output
        assert "Cannot start capture" in result.output
        assert "Traceback" not in result.output, (
            f"B11 regression: a traceback reached the user:\n{result.output}"
        )
        # 83 lines was the measured "before". Allow generous room for terminal
        # wrapping while still failing if a traceback comes back.
        assert len(result.output.splitlines()) < 15, (
            f"expected a short actionable message, got "
            f"{len(result.output.splitlines())} lines:\n{result.output}"
        )
