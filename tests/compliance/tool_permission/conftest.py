# Copyright 2024 NovaFabric Contributors
# Apache-2.0 License
"""Fixtures for tool permission event tests."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from novafabric.compliance.tool_permission.models import ToolPermissionEvent


def _make_event(
    *,
    capsule_id: str = "01HTEST000000000000000000A",
    tool_name: str = "bash",
    tool_id: str = "tool:bash:v1",
    policy_id: str = "policy/default.rego",
    permission_level: str = "execute",
    decision: str = "allowed",
    authorising_identity: str = "user:alice",
    human_approval_required: bool = False,
    human_approval_obtained: bool | None = None,
    approval_principal: str | None = None,
    decision_latency_ms: int = 5,
) -> ToolPermissionEvent:
    return ToolPermissionEvent(
        capsule_id=capsule_id,
        event_id=str(uuid.uuid4()),
        tool_name=tool_name,
        tool_id=tool_id,
        policy_id=policy_id,
        permission_level=permission_level,
        decision=decision,
        authorising_identity=authorising_identity,
        human_approval_required=human_approval_required,
        human_approval_obtained=human_approval_obtained,
        approval_principal=approval_principal,
        decision_latency_ms=decision_latency_ms,
        capsule_timestamp_utc=datetime.now(timezone.utc).isoformat(),
    )


@pytest.fixture
def allowed_event() -> ToolPermissionEvent:
    return _make_event(decision="allowed", permission_level="execute")


@pytest.fixture
def denied_event() -> ToolPermissionEvent:
    return _make_event(decision="denied", permission_level="filesystem")


@pytest.fixture
def escalated_event() -> ToolPermissionEvent:
    return _make_event(
        decision="escalated_to_human",
        permission_level="escalated",
        human_approval_required=True,
        human_approval_obtained=True,
        approval_principal="user:security-team",
    )
