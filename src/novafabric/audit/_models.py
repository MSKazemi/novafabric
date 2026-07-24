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
    ALERT_DELIVERY = "alert.delivery"  # ADR-0192: one entry per attempted alert delivery
    API_KEY_CREATE = "api_key.create"  # ADR-0193
    API_KEY_REVOKE = "api_key.revoke"  # ADR-0193
    API_KEY_ROTATE = "api_key.rotate"  # ADR-0193 (slice 2)
    CAPSULE_IMPORT = "capsule.import"  # ADR-0207: one entry per `nova import` run
    ERASURE_REQUEST = "erasure.request"  # ADR-0210: one entry per REST erasure request
    # ADR-0205: webhook subscription registry lifecycle + delivery trail
    WEBHOOK_CREATE = "webhook.create"
    WEBHOOK_UPDATE = "webhook.update"
    WEBHOOK_DELETE = "webhook.delete"
    WEBHOOK_PING = "webhook.ping"
    WEBHOOK_REDELIVER = "webhook.redeliver"
    WEBHOOK_DELIVERY = "webhook.delivery"  # one entry per attempted delivery
    WEBHOOK_QUEUE_OVERFLOW = "webhook.queue.overflow"  # one per bounded window


class AuditEntry(BaseModel):
    entry_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: datetime
    event_type: AuditEventType
    actor: str
    resource_id: str
    details: dict[str, Any] = Field(default_factory=dict)
    prev_hash: str | None = None
    entry_hash: str = ""
