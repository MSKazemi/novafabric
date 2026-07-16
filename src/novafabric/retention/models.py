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
"""Retention-scheduler data models (ADR-0134, spec retention-scheduler-v0).

Two persisted artifacts, both additive and optional:

* :class:`RetentionBinding` — one row of the optional ``bindings:`` block in
  the ADR-0031 ``retention-policy.yaml``.
* :class:`RetentionActionRecord` — one append-only evidence entry per sweep
  decision ("deletion is evidence"), carried inside a hash-chained
  :class:`novafabric.audit.AuditEntry`.

Plus in-memory sweep types (:class:`SweepItem`, :class:`PlannedDecision`).
"""

from __future__ import annotations

import enum
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from novafabric.capture._ulid import new_ulid
from novafabric.retention.windows import parse_iso_duration, parse_window


class RetentionAction(str, enum.Enum):
    """Terminal action on a due item; maps 1:1 onto an existing mechanism."""

    EXPIRE_METADATA = "expire-metadata"
    PURGE = "purge"
    CRYPTO_SHRED = "crypto-shred"


#: Destructiveness order used for multi-binding conflict resolution
#: (least destructive first; spec "Edge cases").
ACTION_SEVERITY: dict[RetentionAction, int] = {
    RetentionAction.EXPIRE_METADATA: 0,
    RetentionAction.CRYPTO_SHRED: 1,
    RetentionAction.PURGE: 2,
}


class ItemKind(str, enum.Enum):
    CAPSULE = "capsule"
    ASSET = "asset"


class SweepOutcome(str, enum.Enum):
    APPLIED = "applied"
    DRY_RUN = "dry-run"
    SKIPPED = "skipped"
    DEFERRED = "deferred"
    ERROR = "error"


class RetentionMatch(BaseModel):
    """Selector choosing governed items. Predicates are ANDed.

    An empty ``match`` is rejected (fail-safe: it would match nothing).
    """

    model_config = ConfigDict(extra="forbid")

    asset: str | list[str] | None = None
    deployment_environment: str | list[str] | None = None
    tag: str | list[str] | None = None
    age: str | None = None

    @field_validator("age")
    @classmethod
    def _age_is_duration(cls, v: str | None) -> str | None:
        if v is not None:
            parse_iso_duration(v)  # raises WindowParseError on malformed input
        return v

    @model_validator(mode="after")
    def _at_least_one_predicate(self) -> RetentionMatch:
        if all(
            getattr(self, name) is None
            for name in ("asset", "deployment_environment", "tag", "age")
        ):
            raise ValueError("match must contain at least one predicate (fail-safe)")
        return self


class RetentionBinding(BaseModel):
    """One policy -> item -> action binding under ``retention-policy.yaml`` ``bindings:``."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., min_length=1)
    match: RetentionMatch
    window: str
    action: RetentionAction
    legal_hold: bool = False
    description: str | None = None
    enabled: bool = True

    @field_validator("window")
    @classmethod
    def _window_is_valid(cls, v: str) -> str:
        parse_window(v)  # raises WindowParseError on malformed input
        return v


class RetentionActionRecord(BaseModel):
    """One append-only evidence entry per sweep decision (who/what/when/why).

    Serialise with ``model_dump(mode="json", exclude_none=True)`` to stay valid
    against ``schemas/retention-action-record.schema.json``.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["0.1.0"] = "0.1.0"
    record_id: str = Field(
        default_factory=new_ulid, pattern=r"^[0-7][0-9A-HJKMNP-TV-Z]{25}$"
    )
    swept_at: datetime
    principal: str = Field(..., min_length=1)
    registry: str = Field(..., min_length=1)
    binding_id: str = Field(..., min_length=1)
    item_kind: ItemKind
    item_id: str = Field(..., min_length=1)
    action: RetentionAction
    outcome: SweepOutcome
    due_at: datetime
    reason: str | None = None
    worm_locked_until: datetime | None = None
    erasure_receipt_ref: str | None = None
    dual_object: dict[str, Any] | None = None
    description: str | None = None


class SweepItem(BaseModel):
    """One sweepable item (a local capsule) with the attributes bindings match on."""

    item_id: str
    item_kind: ItemKind = ItemKind.CAPSULE
    created_at: datetime
    path: Path | None = None
    deployment_environment: str | None = None
    tags: set[str] = Field(default_factory=set)
    asset_ids: list[str] = Field(default_factory=list)
    pii_subject_id: str | None = None
    expired: bool = False


class Decision(str, enum.Enum):
    """Planner verdict for one item (pre-dispatch)."""

    DUE = "due"
    HELD = "held"
    ERROR = "error"


class PlannedDecision(BaseModel):
    """One row of a sweep plan: the item, the resolved action, and the verdict."""

    item: SweepItem
    binding_id: str
    matched_binding_ids: list[str]
    action: RetentionAction
    due_at: datetime
    decision: Decision
    reason: str | None = None
    worm_locked_until: datetime | None = None
    description: str | None = None
