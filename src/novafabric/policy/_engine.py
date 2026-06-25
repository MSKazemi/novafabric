from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Protocol

from ._models import PolicyDecision, PolicyInput

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ToolPermissionEvent recording hook (cap-004)
#
# This hook is OBSERVATIONAL: errors in recording are logged and never alter
# the policy decision.  The hook is injected at each PolicyDecision return
# point by callers that hold a capsule_id context.
# ---------------------------------------------------------------------------


def record_tool_permission_event(
    decision: PolicyDecision,
    input_: PolicyInput,
    capsule_id: str,
    decision_latency_ms: int = 0,
    db_path: Any = None,
) -> None:
    """Record a :class:`ToolPermissionEvent` for *decision* into the index.

    This function is designed to be called immediately after a ``PolicyDecision``
    is produced.  It is entirely OBSERVATIONAL — any exception is caught, logged
    at WARNING level, and swallowed so it never alters the policy decision.

    Args:
        decision:             The policy decision that was reached.
        input_:               The policy input that produced *decision*.
        capsule_id:           Run capsule ULID — used as the owning capsule.
        decision_latency_ms:  Wall-clock latency of the decision (ms).
        db_path:              Optional path to the PermissionEventIndex SQLite DB.
                              If ``None`` or the module is unavailable, the event
                              is only emitted as a structured log record.
    """
    try:
        from pathlib import Path

        from novafabric.compliance.tool_permission.models import (
            PERMISSION_DECISION_ALLOWED,
            PERMISSION_DECISION_DENIED,
            VALID_PERMISSION_LEVELS,
            ToolPermissionEvent,
        )

        # Map PolicyDecision.allow → PermissionDecision string.
        if decision.allow:
            perm_decision = PERMISSION_DECISION_ALLOWED
        else:
            perm_decision = PERMISSION_DECISION_DENIED

        # Derive permission_level from PolicyResource.kind or context.
        action = input_.action.lower()
        kind = input_.resource.kind.lower()
        context = input_.context

        # Try to extract a richer permission_level from context if provided.
        level_hint: str = context.get("permission_level", "")
        if level_hint in VALID_PERMISSION_LEVELS:
            perm_level = level_hint
        elif "network" in action or "network" in kind:
            perm_level = "network"
        elif "filesystem" in action or "file" in kind:
            perm_level = "filesystem"
        elif "exec" in action:
            perm_level = "execute"
        elif "write" in action:
            perm_level = "write"
        elif "read" in action:
            perm_level = "read"
        else:
            perm_level = "execute"  # conservative default

        human_required = context.get("human_approval_required", False)
        human_obtained = context.get("human_approval_obtained", None)
        approval_principal = context.get("approval_principal", None)

        event = ToolPermissionEvent(
            entity_type="tool_permission_event",
            capsule_id=capsule_id,
            event_id=str(uuid.uuid4()),
            tool_name=context.get("tool_name", input_.resource.ref),
            tool_id=context.get("tool_id", input_.resource.ref),
            policy_id=decision.policy_path or "unknown",
            permission_level=perm_level,
            decision=perm_decision,
            authorising_identity=input_.subject.user,
            human_approval_required=bool(human_required),
            human_approval_obtained=human_obtained,
            approval_principal=approval_principal,
            decision_latency_ms=decision_latency_ms,
            capsule_timestamp_utc=datetime.now(timezone.utc).isoformat(),
        )

        logger.info(
            "tool_permission_event_recorded",
            extra={
                "event": "tool_permission_event_recorded",
                "capsule_id": capsule_id,
                "event_id": event.event_id,
                "tool_name": event.tool_name,
                "decision": perm_decision,
                "permission_level": perm_level,
            },
        )

        # Optionally persist to SQLite index.
        if db_path is not None:
            from novafabric.compliance.tool_permission.index import PermissionEventIndex
            idx = PermissionEventIndex(db_path=Path(db_path))
            with idx:
                idx.record(event)

    except Exception as exc:
        # OBSERVATIONAL — errors must NOT alter policy decisions.
        logger.warning(
            "tool_permission_event_recording_failed: %s", exc, exc_info=True
        )


class PolicyEngine(Protocol):
    def evaluate(self, input_: PolicyInput) -> PolicyDecision: ...
