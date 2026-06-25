from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import yaml
from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()


def _make_capsule(
    parent: Path,
    run_id: str = "01FAKEULIDTEST001",
    exit_code: int = 0,
    with_lineage: bool = True,
) -> Path:
    capsule = parent / run_id
    capsule.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {
        "run_id": run_id,
        "command": [sys.executable, "-c", "pass"],
        "created_at": "2026-05-08T12:00:00.000000Z",
        "finished_at": "2026-05-08T12:01:00.000000Z",
        "exit_code": exit_code,
        "status": "success" if exit_code == 0 else "failure",
    }
    (capsule / "capsule.yaml").write_text(yaml.dump(manifest))
    if with_lineage:
        (capsule / "lineage.jsonl").write_text("")
    return capsule


def test_emit_openlineage_single_capsule_to_file(tmp_path: Path) -> None:
    capsule = _make_capsule(tmp_path)
    out_file = tmp_path / "out.ndjson"
    result = runner.invoke(app, ["lineage", "emit-openlineage", str(capsule),
                                  "--output", str(out_file)])
    assert result.exit_code == 0
    lines = [ln for ln in out_file.read_text().splitlines() if ln.strip()]
    assert len(lines) == 2
    assert json.loads(lines[0])["eventType"] == "START"
    assert json.loads(lines[1])["eventType"] == "COMPLETE"


def test_emit_openlineage_runs_dir_to_file(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    _make_capsule(runs, "01RUN001")
    _make_capsule(runs, "01RUN002")
    out_file = tmp_path / "out.ndjson"
    result = runner.invoke(app, ["lineage", "emit-openlineage", str(runs),
                                  "--output", str(out_file)])
    assert result.exit_code == 0
    lines = [ln for ln in out_file.read_text().splitlines() if ln.strip()]
    assert len(lines) == 4  # 2 events × 2 capsules


def test_emit_openlineage_stdout(tmp_path: Path) -> None:
    capsule = _make_capsule(tmp_path)
    result = runner.invoke(app, ["lineage", "emit-openlineage", str(capsule),
                                  "--output", "-"])
    assert result.exit_code == 0
    lines = [ln for ln in result.output.splitlines() if ln.strip()]
    assert len(lines) == 2
    assert json.loads(lines[0])["eventType"] == "START"
    assert json.loads(lines[1])["eventType"] == "COMPLETE"


def test_emit_openlineage_exits_0_on_success(tmp_path: Path) -> None:
    capsule = _make_capsule(tmp_path)
    result = runner.invoke(app, ["lineage", "emit-openlineage", str(capsule)])
    assert result.exit_code == 0


def test_emit_openlineage_skips_capsule_without_lineage_jsonl(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    _make_capsule(runs, "01RUN001", with_lineage=False)
    out_file = tmp_path / "out.ndjson"
    result = runner.invoke(app, ["lineage", "emit-openlineage", str(runs),
                                  "--output", str(out_file)])
    assert result.exit_code == 0
    lines = [ln for ln in out_file.read_text().splitlines() if ln.strip()]
    # Events emitted with empty inputs/outputs (lineage.jsonl absent = graceful)
    assert len(lines) == 2
    complete = json.loads(lines[1])
    assert complete["inputs"] == []
    assert complete["outputs"] == []
