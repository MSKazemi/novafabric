# tests/test_cli_lineage.py
from __future__ import annotations

import json
import sys
from pathlib import Path

from typer.testing import CliRunner

from novafabric.capture.orchestrator import CaptureOrchestrator
from novafabric.cli.main import app

runner = CliRunner()


def _make_indexed_capsule(tmp_path: Path) -> Path:
    """Capture a run and index it into a local DB."""
    import os
    db_path = tmp_path / "test.db"
    os.environ["NOVAFABRIC_DB_PATH"] = str(db_path)
    try:
        orch = CaptureOrchestrator(base_dir=tmp_path / "runs")
        result = orch.run(command=[sys.executable, "-c", "pass"])
        return result.capsule_dir
    finally:
        del os.environ["NOVAFABRIC_DB_PATH"]


def test_lineage_import_single_capsule(tmp_path: Path) -> None:
    import os
    db_path = tmp_path / "test.db"
    os.environ["NOVAFABRIC_DB_PATH"] = str(db_path)
    try:
        orch = CaptureOrchestrator(base_dir=tmp_path / "runs")
        result = orch.run(command=[sys.executable, "-c", "pass"])
        cap = result.capsule_dir
        cli_result = runner.invoke(app, ["lineage", "import", str(cap)])
        assert cli_result.exit_code == 0
    finally:
        del os.environ["NOVAFABRIC_DB_PATH"]


def test_lineage_import_runs_dir(tmp_path: Path) -> None:
    import os
    db_path = tmp_path / "test.db"
    os.environ["NOVAFABRIC_DB_PATH"] = str(db_path)
    try:
        orch = CaptureOrchestrator(base_dir=tmp_path / "runs")
        orch.run(command=[sys.executable, "-c", "pass"])
        orch.run(command=[sys.executable, "-c", "pass"])
        runs_dir = tmp_path / "runs"
        cli_result = runner.invoke(app, ["lineage", "import", str(runs_dir)])
        assert cli_result.exit_code == 0
        assert "indexed" in cli_result.output
    finally:
        del os.environ["NOVAFABRIC_DB_PATH"]


def test_lineage_provenance_exits_1_for_unknown_ref(tmp_path: Path) -> None:
    import os
    db_path = tmp_path / "test.db"
    os.environ["NOVAFABRIC_DB_PATH"] = str(db_path)
    try:
        cli_result = runner.invoke(app, ["lineage", "provenance", "nonexistent-ref"])
        assert cli_result.exit_code == 1
    finally:
        del os.environ["NOVAFABRIC_DB_PATH"]


def test_lineage_provenance_json_output_is_valid(tmp_path: Path) -> None:
    import os
    db_path = tmp_path / "test.db"
    os.environ["NOVAFABRIC_DB_PATH"] = str(db_path)
    try:
        orch = CaptureOrchestrator(base_dir=tmp_path / "runs")
        result = orch.run(command=[sys.executable, "-c", "pass"])
        run_id = result.run_id
        cli_result = runner.invoke(
            app, ["lineage", "provenance", run_id, "--output", "json"]
        )
        assert cli_result.exit_code == 0
        json.loads(cli_result.output)  # must be valid JSON
    finally:
        del os.environ["NOVAFABRIC_DB_PATH"]


def test_lineage_replay_chain_empty_for_non_replay(tmp_path: Path) -> None:
    import os
    db_path = tmp_path / "test.db"
    os.environ["NOVAFABRIC_DB_PATH"] = str(db_path)
    try:
        orch = CaptureOrchestrator(base_dir=tmp_path / "runs")
        result = orch.run(command=[sys.executable, "-c", "pass"])
        run_id = result.run_id
        cli_result = runner.invoke(app, ["lineage", "replay-chain", run_id])
        assert cli_result.exit_code == 0
        assert "No replay chain" in cli_result.output
    finally:
        del os.environ["NOVAFABRIC_DB_PATH"]
