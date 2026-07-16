from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class AuditEventType(str, enum.Enum):
    POLICY_ALLOW = "policy.allow"
    POLICY_DENY = "policy.deny"
    PROMOTE = "promote"
    APPROVE = "approve"
    HOLD_CREATE = "hold.create"
    HOLD_RELEASE = "hold.release"
    CAPSULE_DELETE = "capsule.delete"
    EVIDENCE_EXPORT = "evidence.export"
    ROLLBACK = "rollback"
    UNREGISTER = "unregister"
    PROMOTE_PROPOSE = "promote.propose"
    PROMOTE_APPROVE = "promote.approve"
    RETENTION_ACTION = "retention.action"


class AuditEntry(BaseModel):
    entry_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: datetime
    event_type: AuditEventType
    actor: str
    resource_id: str
    details: dict[str, Any] = Field(default_factory=dict)
    prev_hash: str | None = None
    entry_hash: str = ""
