from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel


class LegalHold(BaseModel):
    hold_id: str
    reason: str
    duration_days: int | None = None  # None = indefinite


class RetentionPolicy(BaseModel):
    version: int = 1
    registry: str
    jurisdiction: str = ""
    retention_days: int
    deletion_mode: Literal["defensible", "prohibited"] = "defensible"
    legal_hold_ids: list[str] = []

    @classmethod
    def from_yaml(cls, path: Path) -> "RetentionPolicy":
        data = yaml.safe_load(path.read_text())
        return cls.model_validate(data)


class RetentionPolicyEngine:
    """Computes effective retention and enforces deletion rules.

    Effective retention = max(policy.retention_days, max active hold duration).
    An indefinite hold extends retention to INT_MAX effectively.
    """

    def __init__(self, holds: list[LegalHold] | None = None) -> None:
        self._holds = holds or []

    def effective_retention(self, policy: RetentionPolicy) -> int:
        """Return effective retention in days, accounting for active legal holds."""
        active_holds = [h for h in self._holds if h.hold_id in policy.legal_hold_ids]
        indefinite = any(h.duration_days is None for h in active_holds)
        if indefinite:
            return 10**9  # effectively infinite; no actual deletion window
        hold_days = [h.duration_days for h in active_holds if h.duration_days is not None]
        if hold_days:
            return max(policy.retention_days, max(hold_days))
        return policy.retention_days

    def can_delete(self, policy: RetentionPolicy, age_days: int) -> tuple[bool, str]:
        """Return (allowed, reason). age_days = days since the capsule was written.

        Returns (True, "") if deletion is allowed.
        Returns (False, reason) if deletion is blocked.
        """
        if policy.deletion_mode == "prohibited":
            return False, "deletion_mode=prohibited: capsules in this registry are never deleted"
        active_holds = [h for h in self._holds if h.hold_id in policy.legal_hold_ids]
        if active_holds:
            hold_ids = [h.hold_id for h in active_holds]
            return False, f"active legal holds prevent deletion: {hold_ids}"
        effective = self.effective_retention(policy)
        if age_days < effective:
            return (
                False,
                f"retention window not expired ({age_days} days elapsed, {effective} required)",
            )
        return True, ""
