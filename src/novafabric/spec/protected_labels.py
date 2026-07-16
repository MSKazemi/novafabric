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

"""Protected-label records (ADR-0114, protected-labels v0).

A **protected label** is a deployment label (ADR-0113) whose reassignment is
governed by the existing maker-checker approval flow (ADR-0003/0058). Two
records:

* :class:`LabelProtectionConfig` — the per-``(asset_name, label)`` protection
  setting. Absence of a record (or ``protected=False``) means the label is
  *free* and keeps its one-command ADR-0113 behaviour.
* :class:`PendingLabelMove` — an in-flight, not-yet-applied protected move and
  its ordered :class:`LabelMoveApproval` list. Created by ``propose-move``
  (maker); applied when enough *distinct* checkers approved and the policy
  gate allows.

Invariants (ADR-0114 D1–D3; spec ``design/spec/protected-labels-v0.md``):

* An approval's ``approver`` MUST differ from ``proposed_by`` (no
  self-approval); enforced at the crypto level via the ADR-0058 Ed25519
  keyring fingerprints (``approver_key_fp`` vs ``proposer_key_fp``).
* ``rejected`` and ``expired`` are terminal; ``approved → applied`` is atomic.
* ``signature_ref`` is reserved for NovaSeal (ADR-0041, P3); the Ed25519
  signature material lives in the additive ``*_key_fp`` / ``*_signature``
  fields today.

Schemas: ``schemas/label-protection-config.schema.json`` and
``schemas/protected-label-pending-move.schema.json`` (Draft 2020-12, 0.1.0).
"""

from __future__ import annotations

from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from novafabric.spec.asset_labels import validate_label_name

PROTECTED_LABELS_SCHEMA_VERSION: Final = "0.1.0"

_ULID_PATTERN: Final = r"^[0-7][0-9A-HJKMNP-TV-Z]{25}$"

MoveState = Literal["pending", "approved", "applied", "rejected", "expired"]

#: States from which no further transition is possible.
TERMINAL_MOVE_STATES: Final = frozenset({"applied", "rejected", "expired"})


class LabelProtectionConfig(BaseModel):
    """Per-(asset, label) protection setting (ADR-0114 D1).

    Mirrors ``schemas/label-protection-config.schema.json`` (closed schema).
    The current setting is a projection — the newest row per
    ``(asset_name, label)`` in the append-only ``asset_label_protection``
    table; ``protected=False`` reverts the label to free ADR-0113 behaviour.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["0.1.0"] = PROTECTED_LABELS_SCHEMA_VERSION
    asset_name: str = Field(min_length=1)
    label: str
    protected: bool
    required_approvals: int = Field(default=1, ge=1)
    policy_ref: str | None = None
    created_by: str | None = None
    created_at: str | None = None
    note: str | None = None

    @field_validator("label")
    @classmethod
    def _validate_label(cls, v: str) -> str:
        return validate_label_name(v)


class LabelMoveApproval(BaseModel):
    """One checker decision recorded on a pending move (ADR-0114 D2)."""

    model_config = ConfigDict(extra="forbid")

    approver: str = Field(min_length=1)
    approved_at: str
    decision: Literal["approve", "reject"]
    approver_key_fp: str | None = None
    approver_signature: str | None = None
    signature_ref: str | None = None
    note: str | None = None


class PendingLabelMove(BaseModel):
    """An in-flight protected move + its approvals (ADR-0114 D2).

    Mirrors ``schemas/protected-label-pending-move.schema.json`` (closed
    schema). ``policy_ref`` / ``required_approvals`` are snapshots taken at
    propose time so the gate is stable across the move's lifetime.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["0.1.0"] = PROTECTED_LABELS_SCHEMA_VERSION
    move_id: str = Field(pattern=_ULID_PATTERN)
    asset_name: str = Field(min_length=1)
    label: str
    from_version: str | None
    proposed_version: str = Field(min_length=1)
    proposed_by: str = Field(min_length=1)
    proposed_at: str
    state: MoveState
    approvals: list[LabelMoveApproval]
    proposer_key_fp: str | None = None
    proposer_signature: str | None = None
    reason: str | None = None
    policy_ref: str | None = None
    required_approvals: int | None = Field(default=None, ge=1)
    expires_at: str | None = None
    evidence_ref: str | None = None
    applied_at: str | None = None

    @field_validator("label")
    @classmethod
    def _validate_label(cls, v: str) -> str:
        return validate_label_name(v)
