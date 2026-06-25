# Copyright 2024 NovaFabric Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""ToolPermissionEvent Pydantic model (cap-004)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

# Use string literals directly for better Pydantic v2 compatibility
PERMISSION_LEVEL_READ = "read"
PERMISSION_LEVEL_WRITE = "write"
PERMISSION_LEVEL_EXECUTE = "execute"
PERMISSION_LEVEL_NETWORK = "network"
PERMISSION_LEVEL_FILESYSTEM = "filesystem"
PERMISSION_LEVEL_ESCALATED = "escalated"

VALID_PERMISSION_LEVELS = frozenset(
    {
        PERMISSION_LEVEL_READ,
        PERMISSION_LEVEL_WRITE,
        PERMISSION_LEVEL_EXECUTE,
        PERMISSION_LEVEL_NETWORK,
        PERMISSION_LEVEL_FILESYSTEM,
        PERMISSION_LEVEL_ESCALATED,
    }
)

PERMISSION_DECISION_ALLOWED = "allowed"
PERMISSION_DECISION_DENIED = "denied"
PERMISSION_DECISION_ESCALATED_TO_HUMAN = "escalated_to_human"

VALID_PERMISSION_DECISIONS = frozenset(
    {
        PERMISSION_DECISION_ALLOWED,
        PERMISSION_DECISION_DENIED,
        PERMISSION_DECISION_ESCALATED_TO_HUMAN,
    }
)


class PermissionLevel(str):
    """Namespace for permission level constants (supports PermissionLevel.read etc.)."""

    read = PERMISSION_LEVEL_READ
    write = PERMISSION_LEVEL_WRITE
    execute = PERMISSION_LEVEL_EXECUTE
    network = PERMISSION_LEVEL_NETWORK
    filesystem = PERMISSION_LEVEL_FILESYSTEM
    escalated = PERMISSION_LEVEL_ESCALATED


class PermissionDecision(str):
    """Namespace for permission decision constants (supports PermissionDecision.allowed etc.)."""

    allowed = PERMISSION_DECISION_ALLOWED
    denied = PERMISSION_DECISION_DENIED
    escalated_to_human = PERMISSION_DECISION_ESCALATED_TO_HUMAN


class ToolPermissionEvent(BaseModel):
    """Compliance event emitted at each tool-permission decision (cap-004).

    Every instance is included in the DSSE signing scope so that post-hoc
    tampering of a permission decision is detectable.

    Fields are aligned with EU AI Act Annex IV §11 (human oversight measures)
    and NIS2 Art. 23 (incident actions_taken evidence).
    """

    entity_type: Literal["tool_permission_event"] = "tool_permission_event"
    capsule_id: str = Field(..., description="Run capsule ULID that owns this event")
    event_id: str = Field(..., description="Stable UUID for this decision instance")
    tool_name: str = Field(..., description="Human-readable tool name")
    tool_id: str = Field(..., description="Stable tool identifier (URI or UUID)")
    policy_id: str = Field(..., description="Policy document that produced the decision")
    permission_level: str = Field(
        ...,
        description=(
            "Level of access requested: read|write|execute|network|filesystem|escalated"
        ),
    )
    decision: str = Field(
        ...,
        description="Outcome: allowed|denied|escalated_to_human",
    )
    authorising_identity: str = Field(
        ...,
        description="Principal that the policy engine resolved as the authorising identity",
    )
    human_approval_required: bool = Field(
        ...,
        description="True if the policy required a human-in-the-loop approval",
    )
    human_approval_obtained: bool | None = Field(
        default=None,
        description="True if human approval was obtained; None if not required",
    )
    approval_principal: str | None = Field(
        default=None,
        description="Identity of the human approver; None if not required",
    )
    decision_latency_ms: int = Field(
        ...,
        ge=0,
        description="Wall-clock latency of the policy decision in milliseconds",
    )
    capsule_timestamp_utc: str = Field(
        ...,
        description="ISO 8601 timestamp of the decision (UTC)",
    )

    @field_validator("permission_level")
    @classmethod
    def _validate_permission_level(cls, v: str) -> str:
        if v not in VALID_PERMISSION_LEVELS:
            raise ValueError(
                f"permission_level must be one of {sorted(VALID_PERMISSION_LEVELS)}, got {v!r}"
            )
        return v

    @field_validator("decision")
    @classmethod
    def _validate_decision(cls, v: str) -> str:
        if v not in VALID_PERMISSION_DECISIONS:
            raise ValueError(
                f"decision must be one of {sorted(VALID_PERMISSION_DECISIONS)}, got {v!r}"
            )
        return v

    model_config = {"frozen": True}
