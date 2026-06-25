# tests/test_lineage_capture_integration.py
from __future__ import annotations

import sys
from pathlib import Path

from novafabric.capture.orchestrator import CaptureOrchestrator


def test_capture_writes_lineage_jsonl(tmp_path: Path) -> None:
    orch = CaptureOrchestrator(base_dir=tmp_path / "runs")
    result = orch.run(command=[sys.executable, "-c", "pass"])
    lineage_path = result.capsule_dir / "lineage.jsonl"
    assert lineage_path.exists()


def test_capture_lineage_is_valid_json(tmp_path: Path) -> None:
    import json
    orch = CaptureOrchestrator(base_dir=tmp_path / "runs")
    result = orch.run(command=[sys.executable, "-c", "pass"])
    lineage_path = result.capsule_dir / "lineage.jsonl"
    for line in lineage_path.read_text().splitlines():
        if line.strip():
            json.loads(line)  # raises if invalid


def test_capture_succeeds_even_if_db_path_is_unwritable(tmp_path: Path) -> None:
    import os
    # Point NOVAFABRIC_DB_PATH to an unwritable location
    os.environ["NOVAFABRIC_DB_PATH"] = "/root/no-permission.db"
    try:
        orch = CaptureOrchestrator(base_dir=tmp_path / "runs")
        result = orch.run(command=[sys.executable, "-c", "pass"])
        # Capture should succeed — DB failure is a warning, not fatal
        assert result.exit_code == 0
        assert (result.capsule_dir / "lineage.jsonl").exists()
    finally:
        del os.environ["NOVAFABRIC_DB_PATH"]


def test_capture_emits_openlineage_events_to_file_when_env_set(tmp_path: Path) -> None:
    import json
    import os
    ol_file = tmp_path / "ol.ndjson"
    os.environ["OPENLINEAGE_FILE"] = str(ol_file)
    try:
        orch = CaptureOrchestrator(base_dir=tmp_path / "runs")
        orch.run(command=[sys.executable, "-c", "pass"])
        assert ol_file.exists()
        lines = [line for line in ol_file.read_text().splitlines() if line.strip()]
        assert len(lines) >= 2
        event_types = [json.loads(line)["eventType"] for line in lines]
        assert "START" in event_types
        assert "COMPLETE" in event_types
    finally:
        del os.environ["OPENLINEAGE_FILE"]


def test_capture_succeeds_when_openlineage_url_unreachable(tmp_path: Path) -> None:
    import os
    os.environ["OPENLINEAGE_URL"] = "http://localhost:19999"
    try:
        orch = CaptureOrchestrator(base_dir=tmp_path / "runs")
        result = orch.run(command=[sys.executable, "-c", "pass"])
        assert result.exit_code == 0
    finally:
        del os.environ["OPENLINEAGE_URL"]
