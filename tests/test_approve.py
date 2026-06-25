"""Tests for multi-sign-off approval (Track P-3)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from novafabric.audit._models import AuditEventType
from novafabric.registry.service import (
    AssetNotFoundError,
    InvalidLifecycleTransitionError,
    approve_asset,
    promote_asset,
    register_asset,
)
from novafabric.registry.store import get_connection, init_schema
from novafabric.spec.models import AssetStatus
from novafabric.spec.validator import validate_spec

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _register_model(tmp_db: Path, fixtures_dir: Path) -> None:
    spec = validate_spec(fixtures_dir / "valid_model.yaml")
    register_asset(spec, fixtures_dir / "valid_model.yaml", db_path=tmp_db)


def _promote_to_pending_approval(tmp_db: Path) -> None:
    """Advance fraud-model 1.0.0 through development → validated → pending_approval."""
    promote_asset("fraud-model", "1.0.0", AssetStatus.validated, "test-user", db_path=tmp_db)
    promote_asset(
        "fraud-model", "1.0.0", AssetStatus.pending_approval, "test-user", db_path=tmp_db
    )


# ---------------------------------------------------------------------------
# AssetStatus.pending_approval value
# ---------------------------------------------------------------------------


def test_pending_approval_is_valid_status() -> None:
    assert AssetStatus.pending_approval.value == "pending_approval"
    assert AssetStatus.pending_approval in AssetStatus


def test_validated_is_valid_status() -> None:
    """validated was also added in this track as a lifecycle step."""
    assert AssetStatus.validated.value == "validated"
    assert AssetStatus.validated in AssetStatus


# ---------------------------------------------------------------------------
# approve_asset() — happy path
# ---------------------------------------------------------------------------


def test_approve_records_sign_off(
    tmp_db: Path,
    fixtures_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audit_path = tmp_path / "audit.jsonl"
    monkeypatch.setattr("novafabric.registry.service.AUDIT_LOG_PATH", audit_path)

    _register_model(tmp_db, fixtures_dir)
    _promote_to_pending_approval(tmp_db)
    result = approve_asset("fraud-model", "1.0.0", approver="alice", db_path=tmp_db)

    assert result["asset_name"] == "fraud-model"
    assert result["asset_version"] == "1.0.0"
    assert result["approver"] == "alice"
    assert "approved_at" in result
    assert "approval_id" in result
    assert len(result["approval_id"]) > 0


def test_approve_multiple_sign_offs_allowed(
    tmp_db: Path,
    fixtures_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Multiple approvers can each record a sign-off independently."""
    audit_path = tmp_path / "audit.jsonl"
    monkeypatch.setattr("novafabric.registry.service.AUDIT_LOG_PATH", audit_path)

    _register_model(tmp_db, fixtures_dir)
    _promote_to_pending_approval(tmp_db)
    r1 = approve_asset("fraud-model", "1.0.0", approver="alice", db_path=tmp_db)
    r2 = approve_asset("fraud-model", "1.0.0", approver="bob", note="LGTM", db_path=tmp_db)

    assert r1["approval_id"] != r2["approval_id"]
    assert r2["approver"] == "bob"

    # Both rows must be present in the approvals table
    conn = get_connection(tmp_db)
    init_schema(conn)
    rows = conn.execute(
        "SELECT approver FROM approvals WHERE asset_name = ? AND asset_version = ?",
        ("fraud-model", "1.0.0"),
    ).fetchall()
    conn.close()
    approvers = {row[0] for row in rows}
    assert approvers == {"alice", "bob"}


def test_approve_note_stored(
    tmp_db: Path,
    fixtures_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audit_path = tmp_path / "audit.jsonl"
    monkeypatch.setattr("novafabric.registry.service.AUDIT_LOG_PATH", audit_path)

    _register_model(tmp_db, fixtures_dir)
    _promote_to_pending_approval(tmp_db)
    approve_asset("fraud-model", "1.0.0", approver="carol", note="Looks good", db_path=tmp_db)

    conn = get_connection(tmp_db)
    init_schema(conn)
    row = conn.execute(
        "SELECT note FROM approvals WHERE approver = ?", ("carol",)
    ).fetchone()
    conn.close()
    assert row is not None
    assert row[0] == "Looks good"


# ---------------------------------------------------------------------------
# approve_asset() — error path
# ---------------------------------------------------------------------------


def test_approve_asset_not_found(
    tmp_db: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audit_path = tmp_path / "audit.jsonl"
    monkeypatch.setattr("novafabric.registry.service.AUDIT_LOG_PATH", audit_path)

    with pytest.raises(AssetNotFoundError):
        approve_asset("ghost-model", "9.9.9", approver="alice", db_path=tmp_db)


def test_approve_requires_pending_approval_status(
    tmp_db: Path,
    fixtures_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """approve_asset() must reject an asset that is not in pending_approval status."""
    audit_path = tmp_path / "audit.jsonl"
    monkeypatch.setattr("novafabric.registry.service.AUDIT_LOG_PATH", audit_path)

    # Register an asset — it starts in development status, never promoted.
    _register_model(tmp_db, fixtures_dir)

    with pytest.raises(InvalidLifecycleTransitionError) as exc_info:
        approve_asset("fraud-model", "1.0.0", approver="alice", db_path=tmp_db)

    assert "pending_approval" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Audit log integration
# ---------------------------------------------------------------------------


def test_approve_audit_log_entry(
    tmp_db: Path,
    fixtures_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audit_path = tmp_path / "audit.jsonl"
    monkeypatch.setattr("novafabric.registry.service.AUDIT_LOG_PATH", audit_path)

    _register_model(tmp_db, fixtures_dir)
    _promote_to_pending_approval(tmp_db)
    result = approve_asset(
        "fraud-model", "1.0.0", approver="dave", note="approved", db_path=tmp_db
    )

    assert audit_path.exists(), "audit log must be written"
    all_entries = [
        json.loads(line)
        for line in audit_path.read_text().splitlines()
        if line.strip()
    ]
    approve_entries = [
        e for e in all_entries if e["event_type"] == AuditEventType.APPROVE.value
    ]
    assert len(approve_entries) == 1, (
        f"Expected exactly one APPROVE audit entry, got {len(approve_entries)}"
    )
    entry = approve_entries[0]
    assert entry["event_type"] == AuditEventType.APPROVE.value
    assert entry["actor"] == "dave"
    assert entry["resource_id"] == "fraud-model@1.0.0"
    assert entry["details"]["approval_id"] == result["approval_id"]
    assert entry["details"]["note"] == "approved"


# ---------------------------------------------------------------------------
# Lifecycle transitions
# ---------------------------------------------------------------------------


def test_lifecycle_transition_to_validated(
    tmp_db: Path,
    fixtures_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audit_path = tmp_path / "audit.jsonl"
    monkeypatch.setattr("novafabric.registry.service.AUDIT_LOG_PATH", audit_path)

    _register_model(tmp_db, fixtures_dir)
    result = promote_asset(
        "fraud-model", "1.0.0", AssetStatus.validated, "test-user", db_path=tmp_db
    )
    assert result["status"] == "validated"


def test_lifecycle_transition_to_pending_approval(
    tmp_db: Path,
    fixtures_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audit_path = tmp_path / "audit.jsonl"
    monkeypatch.setattr("novafabric.registry.service.AUDIT_LOG_PATH", audit_path)

    _register_model(tmp_db, fixtures_dir)
    promote_asset("fraud-model", "1.0.0", AssetStatus.validated, "test-user", db_path=tmp_db)
    result = promote_asset(
        "fraud-model", "1.0.0", AssetStatus.pending_approval, "test-user", db_path=tmp_db
    )
    assert result["status"] == "pending_approval"


def test_lifecycle_transition_pending_approval_to_staging(
    tmp_db: Path,
    fixtures_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audit_path = tmp_path / "audit.jsonl"
    monkeypatch.setattr("novafabric.registry.service.AUDIT_LOG_PATH", audit_path)

    _register_model(tmp_db, fixtures_dir)
    promote_asset("fraud-model", "1.0.0", AssetStatus.validated, "test-user", db_path=tmp_db)
    promote_asset(
        "fraud-model", "1.0.0", AssetStatus.pending_approval, "test-user", db_path=tmp_db
    )
    result = promote_asset(
        "fraud-model", "1.0.0", AssetStatus.staging, "test-user", db_path=tmp_db
    )
    assert result["status"] == "staging"
