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

"""Pydantic v2 models for Phase 3 parent/child capsule hierarchy.

cap-001: PARTIALLY_COMPLETE terminal state
cap-002: Typed lineage edge vocabulary
cap-003: Fail-open out-of-order arrival
cap-004: Two-phase capsule lifecycle
cap-006: world_size / expected_children
cap-007: ULID canonical global_run_id

NOTE: global_run_id, parent_run_id, trace_id, span_id field semantics are
imported from src/novafabric/envelope/models.py — do NOT redefine them here.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# ── ID pattern reuse from envelope (do not redefine) ──────────────────────
_ULID_RE = re.compile(r"^[0-7][0-9A-HJKMNP-TV-Z]{25}$")
_UUID_V7_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)

SCHEMA_VERSION = "0.2.0"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Enumerations ──────────────────────────────────────────────────────────


class CapsuleRole(str, Enum):
    """Role of a capsule in the distributed run hierarchy."""

    PARENT = "PARENT"
    CHILD = "CHILD"
    STANDALONE = "STANDALONE"


class DistributionRole(str, Enum):
    """Execution role within a distributed job."""

    DRIVER = "DRIVER"
    WORKER = "WORKER"


class ParentStatus(str, Enum):
    """Terminal and non-terminal states for a parent capsule (cap-001)."""

    RUNNING = "RUNNING"
    COMPLETE = "COMPLETE"
    PARTIALLY_COMPLETE = "PARTIALLY_COMPLETE"
    FAILED = "FAILED"


class ChildStatus(str, Enum):
    """Valid states for a child capsule."""

    RUNNING = "RUNNING"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"


class FailMode(str, Enum):
    """How to handle child failures (cap-001, FR-05)."""

    fail_open = "fail-open"    # default — tolerate partial completion
    fail_closed = "fail-closed"  # demote to FAILED if any child failed


class EdgeType(str, Enum):
    """Typed lineage edge vocabulary (cap-002, FR-07).

    W3C PROV-DM mapping (ADR-0044):
      spawned            → wasGeneratedBy
      contains           → wasInformedBy
      delegated_to       → actedOnBehalfOf
      replayed_from      → NovaFabric-specific (no direct PROV-DM counterpart)
      member_of_session  → hadMember (ADR-0122 D3)
      wrote_memory       → wasGeneratedBy (ADR-0143 P1)
      read_memory        → used (ADR-0143 P1)

    ``member_of_session`` is a **grouping** edge and is deliberately kept
    distinct from the four causal/compositional types above (ADR-0122 D3):
    a session groups N *otherwise-independent* runs performed in sequence,
    whereas contains/spawned/delegated_to describe one execution's internal
    structure. Conflating them would let a session look like a distributed
    job — the exact confusion ADR-0122 §Context exists to prevent.
    """

    contains = "contains"
    spawned = "spawned"
    delegated_to = "delegated_to"
    replayed_from = "replayed_from"
    member_of_session = "member_of_session"
    # ADR-0143 P1: memory provenance. `wrote_memory` is run→item (the run
    # produced this memory item); `read_memory` is item→run (the run consumed
    # it). Two edges rather than one bidirectional type because a poisoned-read
    # back-trace has to distinguish *who wrote* a bad item from *who read* it —
    # collapsing them would make the two indistinguishable in the graph, which
    # is the one query this facet exists to answer.
    wrote_memory = "wrote_memory"
    read_memory = "read_memory"


# ── Validators ───────────────────────────────────────────────────────────


def _validate_ulid_or_uuid7(v: str, field_name: str = "id") -> str:
    if not (_ULID_RE.match(v) or _UUID_V7_RE.match(v)):
        raise ValueError(
            f"{field_name} must be a 26-char ULID or 36-char UUID v7, got {v!r}"
        )
    return v


def _validate_ulid(v: str, field_name: str = "id") -> str:
    if not _ULID_RE.match(v):
        raise ValueError(
            f"{field_name} must be a 26-char Crockford Base32 ULID, got {v!r}"
        )
    return v


# ── ParentCapsule ─────────────────────────────────────────────────────────


class ParentCapsule(BaseModel):
    """Parent capsule — root of a distributed run (cap-001, cap-003, cap-006, cap-007)."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    schema_version: str = SCHEMA_VERSION
    capsule_role: CapsuleRole = CapsuleRole.PARENT

    # IDs — aligned with EventEnvelope v1 (do NOT redefine semantics)
    global_run_id: str
    run_id: str
    parent_run_id: str | None = None  # null for top-level parents

    status: ParentStatus = ParentStatus.RUNNING
    fail_mode: FailMode = FailMode.fail_open

    # cap-001: child arrival counters
    children_expected: int | None = None   # null for dynamic-world
    children_arrived: int = 0

    # cap-006: world_size / expected_children
    world_size: int | None = None
    expected_children: int | None = None   # alias; populated from scheduler env

    # Phase 1 — no network at creation time
    created_at: str = Field(default_factory=_now_iso)
    finished_at: str | None = None

    # cap-004: env-var contract
    distribution_role: DistributionRole | None = None
    rank: int | None = None

    pending_parent_timeout_s: float = 86400.0  # 24 h default

    # OTel correlation — from EventEnvelope v1
    trace_id: str | None = None
    span_id: str | None = None

    @field_validator("global_run_id")
    @classmethod
    def _check_global_run_id(cls, v: str) -> str:
        return _validate_ulid_or_uuid7(v, "global_run_id")

    @field_validator("run_id")
    @classmethod
    def _check_run_id(cls, v: str) -> str:
        return _validate_ulid(v, "run_id")

    @field_validator("parent_run_id")
    @classmethod
    def _check_parent_run_id(cls, v: str | None) -> str | None:
        if v is not None:
            _validate_ulid(v, "parent_run_id")
        return v

    @field_validator("schema_version")
    @classmethod
    def _check_schema_version(cls, v: str) -> str:
        if not v.startswith("0.2."):
            raise ValueError(f"schema_version must be 0.2.x, got {v!r}")
        return v

    @field_validator("children_arrived")
    @classmethod
    def _check_children_arrived(cls, v: int) -> int:
        if v < 0:
            raise ValueError("children_arrived must be >= 0")
        return v

    @model_validator(mode="after")
    def _sync_expected_children(self) -> "ParentCapsule":
        """Keep children_expected and expected_children in sync (aliases)."""
        if self.expected_children is not None and self.children_expected is None:
            object.__setattr__(self, "children_expected", self.expected_children)
        if self.children_expected is not None and self.expected_children is None:
            object.__setattr__(self, "expected_children", self.children_expected)
        return self


# ── ChildCapsule ──────────────────────────────────────────────────────────


class ChildCapsule(BaseModel):
    """Child capsule — one worker node in a distributed run (cap-003, cap-006, cap-007)."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    schema_version: str = SCHEMA_VERSION
    capsule_role: CapsuleRole = CapsuleRole.CHILD

    # IDs
    global_run_id: str
    run_id: str
    parent_run_id: str  # required for child capsules

    status: ChildStatus = ChildStatus.RUNNING

    # cap-006
    rank: int | None = None
    world_size: int | None = None
    distribution_role: DistributionRole | None = None

    created_at: str = Field(default_factory=_now_iso)
    finished_at: str | None = None

    trace_id: str | None = None
    span_id: str | None = None

    @field_validator("global_run_id")
    @classmethod
    def _check_global_run_id(cls, v: str) -> str:
        return _validate_ulid_or_uuid7(v, "global_run_id")

    @field_validator("run_id")
    @classmethod
    def _check_run_id(cls, v: str) -> str:
        return _validate_ulid(v, "run_id")

    @field_validator("parent_run_id")
    @classmethod
    def _check_parent_run_id(cls, v: str) -> str:
        return _validate_ulid(v, "parent_run_id")

    @field_validator("schema_version")
    @classmethod
    def _check_schema_version(cls, v: str) -> str:
        if not v.startswith("0.2."):
            raise ValueError(f"schema_version must be 0.2.x, got {v!r}")
        return v


# ── OrphanPlaceholder ─────────────────────────────────────────────────────


class OrphanPlaceholder(BaseModel):
    """Synthetic ORPHAN_PARENT placeholder (cap-003, FR-14, FR-15)."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    schema_version: str = SCHEMA_VERSION
    capsule_role: CapsuleRole = CapsuleRole.PARENT
    is_synthetic: bool = True

    global_run_id: str
    run_id: str  # deterministic based on parent_run_id of orphan children

    status: ParentStatus = ParentStatus.RUNNING
    created_at: str = Field(default_factory=_now_iso)
    orphan_reason: str = "pending_parent_timeout"

    @field_validator("global_run_id")
    @classmethod
    def _check_global_run_id(cls, v: str) -> str:
        return _validate_ulid_or_uuid7(v, "global_run_id")

    @field_validator("run_id")
    @classmethod
    def _check_run_id(cls, v: str) -> str:
        return _validate_ulid(v, "run_id")


# ── LineageEdgeV2 ─────────────────────────────────────────────────────────


class LineageEdgeV2(BaseModel):
    """Phase 3 typed lineage edge (cap-002, FR-07, FR-10).

    PROV-DM mapping defined in ADR-0044 and prov_mapping.py.
    Pre-0.2.0 schemaless rows are interpreted as edge_type='contains'.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    schema_version: str = SCHEMA_VERSION
    edge_id: str
    edge_type: EdgeType  # REQUIRED — no default (cap-002)

    source_run_id: str
    target_run_id: str

    created_at: str = Field(default_factory=_now_iso)
    attributes: dict[str, Any] | None = None  # cap-002 FR-10

    @field_validator("edge_id")
    @classmethod
    def _check_edge_id(cls, v: str) -> str:
        return _validate_ulid(v, "edge_id")

    @field_validator("source_run_id", "target_run_id")
    @classmethod
    def _check_run_ids(cls, v: str) -> str:
        return _validate_ulid_or_uuid7(v, "run_id")

    @field_validator("schema_version")
    @classmethod
    def _check_schema_version(cls, v: str) -> str:
        if not v.startswith("0.2."):
            raise ValueError(f"schema_version must be 0.2.x, got {v!r}")
        return v
