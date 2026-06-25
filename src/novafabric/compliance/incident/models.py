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
"""Incident entity models (ADR-0088).

The ``Incident`` is the persisted *record*; the NIS2 / OECD AIM documents are
*reports* rendered from it.  Lifecycle is forward-only (``open → reported →
closed``) so a wrongly-opened incident is closed, never deleted.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


class IncidentStatus(str, enum.Enum):
    """Forward-only incident lifecycle states."""

    OPEN = "open"
    REPORTED = "reported"
    CLOSED = "closed"


_STATUS_ORDER: dict[IncidentStatus, int] = {
    IncidentStatus.OPEN: 0,
    IncidentStatus.REPORTED: 1,
    IncidentStatus.CLOSED: 2,
}


class IncidentSeverity(str, enum.Enum):
    """Severity vocabulary shared with the cap-005 NIS2 report."""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class IncidentNotFoundError(KeyError):
    """Raised when an incident id does not exist in the store."""


class IncidentTransitionError(ValueError):
    """Raised on a backward or unknown lifecycle transition."""


class StatusTransition(BaseModel):
    """One audited lifecycle event (status change or attestation attach)."""

    kind: str = Field(..., description="'status' or 'attestation'")
    from_value: str | None = None
    to_value: str
    at: datetime


def new_incident_id() -> str:
    """NovaFabric-assigned incident identifier."""
    return f"inc-{uuid.uuid4().hex[:12]}"


def _require_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None:
        raise ValueError(f"{field_name} must be timezone-aware (UTC)")
    return value.astimezone(timezone.utc)


class Incident(BaseModel):
    """A persisted, lifecycle-tracked incident anchored to run capsules.

    ``aware_at`` is the Art. 73 clock anchor; when unset the clock falls back
    to ``occurred_at``.
    """

    id: str = Field(default_factory=new_incident_id)
    title: str
    classification: str = Field(
        ...,
        description=(
            "Incident taxonomy value (cap-005 vocabulary, e.g. "
            "'unauthorized_tool_use', 'death_of_person', "
            "'widespread_infringement', 'critical_infrastructure_disruption')"
        ),
    )
    severity: IncidentSeverity
    run_ids: list[str] = Field(default_factory=list)
    occurred_at: datetime
    aware_at: datetime | None = None
    status: IncidentStatus = IncidentStatus.OPEN
    attestation_ref: str | None = Field(
        default=None,
        description=(
            "Optional reference to a ReperformanceAttestation (ADR-0087); "
            "semantics activate when ADR-0087 ships"
        ),
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    history: list[StatusTransition] = Field(default_factory=list)
    extensions: dict[str, Any] = Field(
        default_factory=dict,
        description="Additive extension block (distributed-ready framing)",
    )

    @field_validator("occurred_at", "created_at")
    @classmethod
    def _occurred_utc(cls, v: datetime) -> datetime:
        return _require_utc(v, "timestamp")

    @field_validator("aware_at")
    @classmethod
    def _aware_utc(cls, v: datetime | None) -> datetime | None:
        return None if v is None else _require_utc(v, "aware_at")

    @model_validator(mode="after")
    def _aware_not_before_occurred(self) -> "Incident":
        if self.aware_at is not None and self.aware_at < self.occurred_at:
            raise ValueError(
                "aware_at cannot be earlier than occurred_at "
                "(awareness follows occurrence)"
            )
        return self

    @property
    def clock_anchor(self) -> datetime:
        """The Art. 73 deadline anchor: ``aware_at``, else ``occurred_at``."""
        return self.aware_at if self.aware_at is not None else self.occurred_at

    def transitioned(
        self, new_status: IncidentStatus, at: datetime | None = None
    ) -> "Incident":
        """Return a copy in *new_status*; forward-only, audited.

        Raises :class:`IncidentTransitionError` on backward/no-op moves.
        """
        if _STATUS_ORDER[new_status] <= _STATUS_ORDER[self.status]:
            raise IncidentTransitionError(
                f"invalid lifecycle transition {self.status.value} -> "
                f"{new_status.value} (forward-only: open -> reported -> closed)"
            )
        stamp = _require_utc(at or datetime.now(timezone.utc), "at")
        record = StatusTransition(
            kind="status",
            from_value=self.status.value,
            to_value=new_status.value,
            at=stamp,
        )
        return self.model_copy(
            update={"status": new_status, "history": [*self.history, record]}
        )

    def with_attestation(
        self, ref: str, at: datetime | None = None
    ) -> "Incident":
        """Attach a re-performance attestation reference (audited event)."""
        stamp = _require_utc(at or datetime.now(timezone.utc), "at")
        record = StatusTransition(
            kind="attestation", from_value=self.attestation_ref, to_value=ref, at=stamp
        )
        return self.model_copy(
            update={"attestation_ref": ref, "history": [*self.history, record]}
        )
