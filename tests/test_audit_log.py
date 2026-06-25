"""Tests for the tamper-evident audit log (Track P-0, v0.8)."""
from __future__ import annotations

import json
from pathlib import Path

from novafabric.audit import AuditEventType, AuditLog


def test_audit_log_appends_entry(tmp_path: Path) -> None:
    """A single appended entry should produce exactly one JSONL line."""
    log = AuditLog(tmp_path / "audit.jsonl")
    log.append(
        event_type=AuditEventType.PROMOTE,
        actor="alice",
        resource_id="capsule-001",
    )

    lines = (tmp_path / "audit.jsonl").read_text().splitlines()
    assert len(lines) == 1

    d = json.loads(lines[0])
    assert d["event_type"] == "promote"
    assert d["prev_hash"] is None
    assert d["entry_hash"] != ""


def test_audit_log_chain_links_entries(tmp_path: Path) -> None:
    """The second entry's prev_hash must equal the first entry's entry_hash."""
    log = AuditLog(tmp_path / "audit.jsonl")
    log.append(AuditEventType.POLICY_ALLOW, "alice", "capsule-001")
    log.append(AuditEventType.APPROVE, "bob", "capsule-001")

    lines = (tmp_path / "audit.jsonl").read_text().splitlines()
    assert len(lines) == 2

    e1 = json.loads(lines[0])
    e2 = json.loads(lines[1])
    assert e2["prev_hash"] == e1["entry_hash"]


def test_audit_log_detects_tamper(tmp_path: Path) -> None:
    """Tampering with the actor field on line 0 must produce at least one verify error."""
    log_path = tmp_path / "audit.jsonl"
    log = AuditLog(log_path)
    log.append(AuditEventType.PROMOTE, "alice", "capsule-001")
    log.append(AuditEventType.APPROVE, "bob", "capsule-001")

    # Read both lines, tamper the first.
    lines = log_path.read_text().splitlines()
    d = json.loads(lines[0])
    d["actor"] = "mallory"  # tamper

    # Rewrite file with the tampered first line (preserve second line unchanged).
    tampered = json.dumps(d, separators=(",", ":"), sort_keys=True)
    log_path.write_text(tampered + "\n" + lines[1] + "\n", encoding="utf-8")

    fresh_log = AuditLog(log_path)
    errors = fresh_log.verify()
    assert len(errors) > 0
    assert any("entry_hash mismatch" in e or "prev_hash" in e for e in errors), (
        f"Expected hash mismatch error, got: {errors}"
    )


def test_audit_log_verify_clean(tmp_path: Path) -> None:
    """An unmodified log with five entries should verify with no errors."""
    log = AuditLog(tmp_path / "audit.jsonl")
    for i in range(5):
        log.append(AuditEventType.EVIDENCE_EXPORT, f"user-{i}", f"capsule-{i:03d}")

    errors = log.verify()
    assert errors == []
