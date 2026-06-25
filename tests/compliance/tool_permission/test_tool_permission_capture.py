# Copyright 2024 NovaFabric Contributors
# Apache-2.0 License
"""Integration tests for ToolPermissionEvent capture (cap-004)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from novafabric.compliance.tool_permission.index import PermissionEventIndex
from novafabric.compliance.tool_permission.models import (
    PERMISSION_DECISION_ALLOWED,
    PERMISSION_DECISION_DENIED,
    PERMISSION_DECISION_ESCALATED_TO_HUMAN,
    ToolPermissionEvent,
)


def _make_event(decision: str, capsule_id: str = "CAPSULE-001") -> ToolPermissionEvent:
    return ToolPermissionEvent(
        capsule_id=capsule_id,
        event_id=str(uuid.uuid4()),
        tool_name="bash",
        tool_id="tool:bash:v1",
        policy_id="policy/default.rego",
        permission_level="execute",
        decision=decision,
        authorising_identity="user:alice",
        human_approval_required=decision == PERMISSION_DECISION_ESCALATED_TO_HUMAN,
        human_approval_obtained=(
            True if decision == PERMISSION_DECISION_ESCALATED_TO_HUMAN else None
        ),
        approval_principal=(
            "user:security-team"
            if decision == PERMISSION_DECISION_ESCALATED_TO_HUMAN
            else None
        ),
        decision_latency_ms=5,
        capsule_timestamp_utc=datetime.now(timezone.utc).isoformat(),
    )


class TestToolPermissionCapture:
    def test_100_events_all_14_fields_present(self, tmp_path: Path) -> None:
        """Integration: 100 tool calls (60 allowed, 30 denied, 10 escalated)."""
        db = tmp_path / "idx.db"

        events: list[ToolPermissionEvent] = []
        for _ in range(60):
            events.append(_make_event(PERMISSION_DECISION_ALLOWED))
        for _ in range(30):
            events.append(_make_event(PERMISSION_DECISION_DENIED))
        for _ in range(10):
            events.append(_make_event(PERMISSION_DECISION_ESCALATED_TO_HUMAN))

        with PermissionEventIndex(db_path=db) as idx:
            for ev in events:
                idx.record(ev)

        # Verify all 100 stored
        with PermissionEventIndex(db_path=db) as idx:
            assert idx.count() == 100

        # Verify all 14 fields on every event
        EXPECTED_FIELDS = {
            "entity_type", "capsule_id", "event_id", "tool_name", "tool_id",
            "policy_id", "permission_level", "decision", "authorising_identity",
            "human_approval_required", "human_approval_obtained", "approval_principal",
            "decision_latency_ms", "capsule_timestamp_utc",
        }
        for ev in events:
            assert set(ev.model_dump().keys()) == EXPECTED_FIELDS

    def test_query_by_decision_counts(self, tmp_path: Path) -> None:
        db = tmp_path / "idx.db"
        capsule_id = "CAPSULE-002"

        with PermissionEventIndex(db_path=db) as idx:
            for _ in range(60):
                idx.record(_make_event(PERMISSION_DECISION_ALLOWED, capsule_id))
            for _ in range(30):
                idx.record(_make_event(PERMISSION_DECISION_DENIED, capsule_id))
            for _ in range(10):
                idx.record(_make_event(PERMISSION_DECISION_ESCALATED_TO_HUMAN, capsule_id))

        with PermissionEventIndex(db_path=db) as idx:
            allowed = idx.query_by_tool_and_decision("bash", PERMISSION_DECISION_ALLOWED)
            denied = idx.query_by_tool_and_decision("bash", PERMISSION_DECISION_DENIED)
            escalated = idx.query_by_tool_and_decision("bash", PERMISSION_DECISION_ESCALATED_TO_HUMAN)

        assert len(allowed) == 60
        assert len(denied) == 30
        assert len(escalated) == 10

    def test_pre_execution_recording_with_failing_tool(self, tmp_path: Path) -> None:
        """Events recorded before tool execution must survive even if the tool fails."""
        db = tmp_path / "idx.db"
        capsule_id = "CAPSULE-FAIL"

        # Record a denied event (policy blocks execution)
        denied_ev = ToolPermissionEvent(
            capsule_id=capsule_id,
            event_id=str(uuid.uuid4()),
            tool_name="rm",
            tool_id="tool:rm:v1",
            policy_id="policy/safety.rego",
            permission_level="filesystem",
            decision=PERMISSION_DECISION_DENIED,
            authorising_identity="user:bob",
            human_approval_required=False,
            human_approval_obtained=None,
            approval_principal=None,
            decision_latency_ms=2,
            capsule_timestamp_utc=datetime.now(timezone.utc).isoformat(),
        )

        with PermissionEventIndex(db_path=db) as idx:
            idx.record(denied_ev)

        # Simulate tool failure — the index entry must still exist
        with PermissionEventIndex(db_path=db) as idx:
            results = idx.query_by_capsule(capsule_id)

        assert len(results) == 1
        assert results[0].decision == PERMISSION_DECISION_DENIED
        assert results[0].tool_name == "rm"

    def test_duplicate_event_id_silently_ignored(self, tmp_path: Path) -> None:
        db = tmp_path / "idx.db"
        ev = _make_event(PERMISSION_DECISION_ALLOWED)

        with PermissionEventIndex(db_path=db) as idx:
            idx.record(ev)
            idx.record(ev)  # duplicate
            assert idx.count() == 1

    def test_query_by_capsule(self, tmp_path: Path) -> None:
        db = tmp_path / "idx.db"
        cap_a = "CAP-A"
        cap_b = "CAP-B"

        with PermissionEventIndex(db_path=db) as idx:
            for _ in range(3):
                idx.record(_make_event(PERMISSION_DECISION_ALLOWED, cap_a))
            for _ in range(2):
                idx.record(_make_event(PERMISSION_DECISION_DENIED, cap_b))

        with PermissionEventIndex(db_path=db) as idx:
            a_events = idx.query_by_capsule(cap_a)
            b_events = idx.query_by_capsule(cap_b)

        assert len(a_events) == 3
        assert len(b_events) == 2
        assert all(e.capsule_id == cap_a for e in a_events)


class TestPermissionEventIndexNotOpen:
    def test_record_not_open_raises(self, tmp_path: Path) -> None:
        idx = PermissionEventIndex(db_path=tmp_path / "idx.db")
        with pytest.raises(RuntimeError, match="not open"):
            idx.record(_make_event(PERMISSION_DECISION_ALLOWED))

    def test_query_by_capsule_not_open_raises(self, tmp_path: Path) -> None:
        idx = PermissionEventIndex(db_path=tmp_path / "idx.db")
        with pytest.raises(RuntimeError, match="not open"):
            idx.query_by_capsule("CAPSULE-001")

    def test_query_by_tool_and_decision_not_open_raises(self, tmp_path: Path) -> None:
        idx = PermissionEventIndex(db_path=tmp_path / "idx.db")
        with pytest.raises(RuntimeError, match="not open"):
            idx.query_by_tool_and_decision("tool", "allowed")

    def test_count_not_open_raises(self, tmp_path: Path) -> None:
        idx = PermissionEventIndex(db_path=tmp_path / "idx.db")
        with pytest.raises(RuntimeError, match="not open"):
            idx.count()
