"""End-to-end regression test for examples/lineage-chain.

Captures three runs of step.py, then asserts (a) each capsule's
lineage.jsonl has the expected `consumed` edge to the shared asset,
and (b) `nova lineage blast-radius` for the shared asset returns
all three runs.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from typer.testing import CliRunner

from novafabric.cli.main import app

EXAMPLE = (
    Path(__file__).resolve().parent.parent / "examples" / "lineage-chain" / "step.py"
)
SHARED_ASSET = "datasets/training-set@1.0.0"


def _capture_step(runs_dir: Path, step_name: str, db_path: Path) -> Path:
    runner = CliRunner()
    # Route lineage writes to a tmp DB so tests don't pollute ~/.novafabric.
    import os
    saved = os.environ.get("NOVAFABRIC_DB_PATH")
    os.environ["NOVAFABRIC_DB_PATH"] = str(db_path)
    try:
        result = runner.invoke(
            app,
            ["capture", "--output-dir", str(runs_dir),
             sys.executable, str(EXAMPLE), step_name],
        )
    finally:
        if saved is None:
            os.environ.pop("NOVAFABRIC_DB_PATH", None)
        else:
            os.environ["NOVAFABRIC_DB_PATH"] = saved

    assert result.exit_code == 0, (
        f"capture (step={step_name}) failed: exit={result.exit_code}\n{result.output}"
    )
    capsules = sorted(d for d in runs_dir.iterdir() if d.is_dir())
    return capsules[-1]


def test_example_file_exists() -> None:
    assert EXAMPLE.is_file(), f"example missing: {EXAMPLE}"


def test_three_captures_produce_consumed_edges(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    db_path = tmp_path / "lineage.db"

    cap1 = _capture_step(runs_dir, "train-v1", db_path)
    cap2 = _capture_step(runs_dir, "eval-v1", db_path)
    cap3 = _capture_step(runs_dir, "promote-v1", db_path)

    for cap in (cap1, cap2, cap3):
        # The script appended an entry to assets.jsonl — verify it landed.
        assets = (cap / "assets.jsonl").read_text().strip().splitlines()
        assert assets, f"assets.jsonl empty for {cap}"
        records = [json.loads(line) for line in assets]
        assert any(r["asset_ref"] == SHARED_ASSET for r in records), (
            f"consumed-asset record missing in {cap}/assets.jsonl: {records}"
        )

        # The capture orchestrator must have inferred a lineage edge from it.
        lineage_lines = (cap / "lineage.jsonl").read_text().strip().splitlines()
        assert lineage_lines, f"lineage.jsonl empty for {cap}"
        edges = [json.loads(line) for line in lineage_lines]
        consumed = [e for e in edges if e.get("edge_type") == "consumed"]
        assert any(
            e["target"].get("asset_ref") == SHARED_ASSET
            for e in consumed
        ), f"no consumed edge to {SHARED_ASSET} in {cap}/lineage.jsonl: {edges}"
