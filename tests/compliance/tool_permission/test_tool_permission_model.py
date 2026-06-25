# Copyright 2024 NovaFabric Contributors
# Apache-2.0 License
"""Unit tests for ToolPermissionEvent Pydantic model (cap-004)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from novafabric.compliance.tool_permission.models import (
    PERMISSION_DECISION_ALLOWED,
    PERMISSION_DECISION_DENIED,
    PERMISSION_DECISION_ESCALATED_TO_HUMAN,
    VALID_PERMISSION_DECISIONS,
    VALID_PERMISSION_LEVELS,
    ToolPermissionEvent,
)


def _base_kwargs() -> dict:
    return {
        "capsule_id": "01HTEST000000000000000000A",
        "event_id": str(uuid.uuid4()),
        "tool_name": "bash",
        "tool_id": "tool:bash:v1",
        "policy_id": "policy/default.rego",
        "permission_level": "execute",
        "decision": "allowed",
        "authorising_identity": "user:alice",
        "human_approval_required": False,
        "human_approval_obtained": None,
        "approval_principal": None,
        "decision_latency_ms": 10,
        "capsule_timestamp_utc": datetime.now(timezone.utc).isoformat(),
    }


class TestToolPermissionEventModel:
    def test_all_14_fields_present(self) -> None:
        event = ToolPermissionEvent(**_base_kwargs())
        # entity_type + 13 explicit fields = 14 total
        d = event.model_dump()
        expected_fields = {
            "entity_type", "capsule_id", "event_id", "tool_name", "tool_id",
            "policy_id", "permission_level", "decision", "authorising_identity",
            "human_approval_required", "human_approval_obtained", "approval_principal",
            "decision_latency_ms", "capsule_timestamp_utc",
        }
        assert expected_fields == set(d.keys())

    def test_entity_type_is_literal(self) -> None:
        event = ToolPermissionEvent(**_base_kwargs())
        assert event.entity_type == "tool_permission_event"

    def test_entity_type_cannot_be_overridden(self) -> None:
        kw = {**_base_kwargs(), "entity_type": "something_else"}
        with pytest.raises(ValidationError):
            ToolPermissionEvent(**kw)

    def test_all_valid_permission_levels(self) -> None:
        for level in VALID_PERMISSION_LEVELS:
            event = ToolPermissionEvent(**{**_base_kwargs(), "permission_level": level})
            assert event.permission_level == level

    def test_invalid_permission_level(self) -> None:
        with pytest.raises(ValidationError):
            ToolPermissionEvent(**{**_base_kwargs(), "permission_level": "root"})

    def test_all_valid_decisions(self) -> None:
        for dec in VALID_PERMISSION_DECISIONS:
            event = ToolPermissionEvent(**{**_base_kwargs(), "decision": dec})
            assert event.decision == dec

    def test_invalid_decision(self) -> None:
        with pytest.raises(ValidationError):
            ToolPermissionEvent(**{**_base_kwargs(), "decision": "maybe"})

    def test_human_approval_fields_nullable(self) -> None:
        event = ToolPermissionEvent(**{
            **_base_kwargs(),
            "human_approval_required": True,
            "human_approval_obtained": True,
            "approval_principal": "user:security-team",
        })
        assert event.human_approval_obtained is True
        assert event.approval_principal == "user:security-team"

    def test_decision_latency_ms_non_negative(self) -> None:
        with pytest.raises(ValidationError):
            ToolPermissionEvent(**{**_base_kwargs(), "decision_latency_ms": -1})

    def test_model_is_frozen(self) -> None:
        event = ToolPermissionEvent(**_base_kwargs())
        with pytest.raises(Exception):  # ValidationError or TypeError
            event.tool_name = "changed"  # type: ignore[misc]

    def test_json_roundtrip(self) -> None:
        event = ToolPermissionEvent(**_base_kwargs())
        restored = ToolPermissionEvent.model_validate_json(event.model_dump_json())
        assert restored == event

    def test_allowed_decision_constant(self) -> None:
        assert PERMISSION_DECISION_ALLOWED == "allowed"

    def test_denied_decision_constant(self) -> None:
        assert PERMISSION_DECISION_DENIED == "denied"

    def test_escalated_decision_constant(self) -> None:
        assert PERMISSION_DECISION_ESCALATED_TO_HUMAN == "escalated_to_human"
