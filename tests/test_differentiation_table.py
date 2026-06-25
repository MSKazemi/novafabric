"""Smoke test: differentiation verification script exits 0."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent


def test_verify_differentiation_table_exits_zero() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/verify_differentiation_table.py", "--format", "json"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    data = json.loads(result.stdout)
    failed = [r for r in data if r["status"] == "FAIL"]
    assert result.returncode == 0, f"Differentiation check failed: {failed}"
    assert len(data) == 10


def test_verify_differentiation_table_json_schema() -> None:
    """Verify each result entry has all required keys."""
    result = subprocess.run(
        [sys.executable, "scripts/verify_differentiation_table.py", "--format", "json"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    assert result.returncode == 0
    data = json.loads(result.stdout)
    required_keys = {"id", "title", "vs", "status", "error", "note"}
    for entry in data:
        assert required_keys == set(entry.keys()), (
            f"Entry {entry.get('id')} missing keys: {required_keys - set(entry.keys())}"
        )
        assert entry["status"] in ("PASS", "FAIL", "NOTE"), (
            f"Invalid status {entry['status']!r} for {entry['id']}"
        )


def test_verify_differentiation_table_ids() -> None:
    """Verify all D-01 through D-10 claim IDs are present."""
    result = subprocess.run(
        [sys.executable, "scripts/verify_differentiation_table.py", "--format", "json"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    assert result.returncode == 0
    data = json.loads(result.stdout)
    ids = {entry["id"] for entry in data}
    expected = {f"D-{i:02d}" for i in range(1, 11)}
    assert ids == expected, f"Missing claim IDs: {expected - ids}"
