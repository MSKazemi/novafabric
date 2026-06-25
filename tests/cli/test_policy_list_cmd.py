from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()


def _seed_policy_db(db_path: Path, rows: int = 2) -> None:
    """Create a minimal promote_policy table and insert *rows* entries."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS promote_policy (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            namespace TEXT NOT NULL DEFAULT 'default',
            version INTEGER NOT NULL,
            bundle_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )"""
    )
    for i in range(1, rows + 1):
        payload = json.dumps({"proposers": ["alice"], "approvers": ["bob"]})
        conn.execute(
            "INSERT INTO promote_policy (namespace, version, bundle_json, created_at) "
            "VALUES (?, ?, ?, ?)",
            ("default", i, payload, f"2026-05-21T10:0{i}:00+00:00"),
        )
    conn.commit()
    conn.close()


# ── Rego bundle listing ───────────────────────────────────────────────────────

def test_policy_list_shows_rego_files(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Built-in Rego bundle files appear in output."""
    result = runner.invoke(app, ["policy", "list"])
    assert result.exit_code == 0
    # The built-in bundle always has at least one .rego file
    assert ".rego" in result.output


def test_policy_list_custom_bundle(tmp_path: Path) -> None:
    """--bundle pointing to a custom dir lists its .rego files."""
    (tmp_path / "my_policy.rego").write_text("package test\nallow := true\n")
    result = runner.invoke(app, ["policy", "list", "--bundle", str(tmp_path)])
    assert result.exit_code == 0
    assert "my_policy.rego" in result.output


def test_policy_list_empty_bundle(tmp_path: Path) -> None:
    """Empty bundle dir prints a warning, does not crash."""
    result = runner.invoke(app, ["policy", "list", "--bundle", str(tmp_path)])
    assert result.exit_code == 0
    assert "No Rego files found" in result.output


# ── Signed promotion policies ─────────────────────────────────────────────────

def test_policy_list_no_db(tmp_path: Path) -> None:
    """When the DB doesn't exist a helpful message is shown, no crash."""
    missing = tmp_path / "nonexistent.db"
    result = runner.invoke(app, ["policy", "list", "--db", str(missing)])
    assert result.exit_code == 0
    assert "No signed promotion policies" in result.output


def test_policy_list_with_policies(tmp_path: Path) -> None:
    """Two seeded policies appear in the table."""
    db = tmp_path / "merkle.db"
    _seed_policy_db(db, rows=2)
    result = runner.invoke(app, ["policy", "list", "--db", str(db)])
    assert result.exit_code == 0
    assert "Version" in result.output
    assert "default" in result.output
    # Both version rows should appear
    assert "1" in result.output
    assert "2" in result.output


def test_policy_list_namespace_filter(tmp_path: Path) -> None:
    """--namespace filters output to the specified namespace."""
    db = tmp_path / "merkle.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        """CREATE TABLE promote_policy (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            namespace TEXT NOT NULL DEFAULT 'default',
            version INTEGER NOT NULL,
            bundle_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )"""
    )
    for ns, ver in [("default", 1), ("staging", 2)]:
        conn.execute(
            "INSERT INTO promote_policy (namespace, version, bundle_json, created_at) "
            "VALUES (?, ?, ?, ?)",
            (ns, ver, "{}", "2026-05-21T10:00:00+00:00"),
        )
    conn.commit()
    conn.close()

    result = runner.invoke(app, ["policy", "list", "--db", str(db), "--namespace", "staging"])
    assert result.exit_code == 0
    # Split output at the Signed Promotion Policies section
    parts = result.output.split("Signed Promotion Policies")
    assert len(parts) == 2, "Expected policy table section"
    policy_section = parts[1]
    assert "staging" in policy_section
    # version 1 belongs to "default" namespace — must not appear in the filtered table
    policy_rows = [ln for ln in policy_section.splitlines() if "│" in ln and "Version" not in ln and "─" not in ln]
    assert not any("│   1 " in row or "│ 1 " in row for row in policy_rows)
