from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()


def test_hold_create(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("novafabric.cli.hold.AUDIT_LOG_PATH", tmp_path / "audit.jsonl")
    result = runner.invoke(app, ["hold", "create", "my-registry", "--reason", "SEC exam 2026"])
    assert result.exit_code == 0, result.output
    assert "hold-" in result.output
    # Verify hold file was written
    holds_file = tmp_path / ".novafabric" / "registries" / "my-registry" / "holds.jsonl"
    assert holds_file.exists()
    record = json.loads(holds_file.read_text().strip())
    assert record["reason"] == "SEC exam 2026"
    assert record["released_at"] is None


def test_hold_list_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["hold", "list", "my-registry"])
    assert result.exit_code == 0, result.output
    assert "No active holds" in result.output


def test_hold_create_and_list(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("novafabric.cli.hold.AUDIT_LOG_PATH", tmp_path / "audit.jsonl")
    runner.invoke(app, ["hold", "create", "r", "--reason", "exam", "--duration-days", "30"])
    result = runner.invoke(app, ["hold", "list", "r"])
    assert result.exit_code == 0, result.output
    assert "exam" in result.output
    assert "30d" in result.output


def test_hold_release(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("novafabric.cli.hold.AUDIT_LOG_PATH", tmp_path / "audit.jsonl")
    create_result = runner.invoke(app, ["hold", "create", "r", "--reason", "test"])
    assert create_result.exit_code == 0, create_result.output
    # Extract hold_id from output
    hold_id = [w for w in create_result.output.split() if w.startswith("hold-")][0]
    release_result = runner.invoke(app, ["hold", "release", hold_id])
    assert release_result.exit_code == 0, release_result.output
    assert "Released" in release_result.output
    # After release, list should show no active holds
    list_result = runner.invoke(app, ["hold", "list", "r"])
    assert "No active holds" in list_result.output


def test_hold_release_not_found(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["hold", "release", "hold-nonexistent"])
    assert result.exit_code == 1


def test_hold_indefinite_duration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("novafabric.cli.hold.AUDIT_LOG_PATH", tmp_path / "audit.jsonl")
    result = runner.invoke(app, ["hold", "create", "r2", "--reason", "indefinite hold"])
    assert result.exit_code == 0, result.output
    assert "indefinite" in result.output
    list_result = runner.invoke(app, ["hold", "list", "r2"])
    assert "indefinite" in list_result.output


def test_capsule_delete_blocked_by_hold(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """capsule delete must be blocked by an active hold, even without a retention-policy.yaml."""
    monkeypatch.chdir(tmp_path)
    audit_path = tmp_path / "audit.jsonl"
    monkeypatch.setattr("novafabric.cli.hold.AUDIT_LOG_PATH", audit_path)
    monkeypatch.setattr("novafabric.cli.capsule.AUDIT_LOG_PATH", audit_path)
    # Create a hold on the registry
    create_result = runner.invoke(
        app, ["hold", "create", "reg1", "--reason", "SEC exam"]
    )
    assert create_result.exit_code == 0, create_result.output
    # Attempt deletion — must be blocked (no retention-policy.yaml present)
    delete_result = runner.invoke(
        app, ["capsule", "delete", "cap-001", "--registry", "reg1"]
    )
    assert delete_result.exit_code == 1
    assert "blocked" in delete_result.output.lower()


def test_hold_audit_log_written(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Both hold.create and hold.release should write entries to the audit log."""
    audit_path = tmp_path / "audit.jsonl"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("novafabric.cli.hold.AUDIT_LOG_PATH", audit_path)
    create_result = runner.invoke(app, ["hold", "create", "r", "--reason", "audit-test"])
    assert create_result.exit_code == 0
    hold_id = [w for w in create_result.output.split() if w.startswith("hold-")][0]
    release_result = runner.invoke(app, ["hold", "release", hold_id])
    assert release_result.exit_code == 0, release_result.output
    assert audit_path.exists()
    entries = [json.loads(line) for line in audit_path.read_text().splitlines() if line.strip()]
    event_types = [e["event_type"] for e in entries]
    assert "hold.create" in event_types
    assert "hold.release" in event_types
