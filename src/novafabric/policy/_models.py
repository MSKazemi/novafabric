from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel


class PolicyDeniedError(Exception):
    """Raised when a policy gate denies an operation."""

    def __init__(self, reason: str, decision_id: str = "") -> None:
        suffix = f" (decision_id={decision_id})" if decision_id else ""
        super().__init__(f"Policy denied: {reason}{suffix}")
        self.reason = reason
        self.decision_id = decision_id


class PolicySubject(BaseModel):
    user: str
    roles: list[str] = []


class PolicyResource(BaseModel):
    kind: str  # "asset" | "capsule" | "replay"
    ref: str
    asset_type: str | None = None  # "agent" | "tool" | "dataset" | "prompt" (kind == "asset")
    eval_score: float | None = None
    redaction_proof_present: bool = False
    unsafe_skips: int = 0
    dataset_ref: str | None = None
    dataset_expires_at: datetime | None = None
    lineage: dict[str, Any] = {}
    regression_report: dict[str, Any] | None = None  # serialized RegressionReport via .model_dump()
    budget: dict[str, Any] | None = None  # recorded budget rollup (ADR-0136) — see _budget.py


class PolicyInput(BaseModel):
    action: str
    subject: PolicySubject
    resource: PolicyResource
    context: dict[str, Any] = {}


class PolicyDecision(BaseModel):
    allow: bool
    reason: str = ""
    decision_id: str = ""
    policy_path: str = ""
    input_snapshot: PolicyInput | None = None
    trace_text: str | None = None
