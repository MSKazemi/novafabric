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

"""Asset deployment-label records (ADR-0113, asset-labels v0).

A **deployment label** is a mutable named pointer ``(asset_name, label) →
target_version`` into the immutable asset version set. Two records:

* :class:`AssetLabelMove` — one append-only audit row per label set/move
  (stored in ``asset_label_history``; the current pointer is a projection —
  the newest row per ``(asset_name, label)``).
* :class:`ResolvedAssetRef` — the capture-time **resolution freeze** written
  into a Run Capsule: the concrete version + content hash a reference
  resolved to. Replay uses the frozen version and never re-resolves the
  label, so a capsule stays deterministic after the label moves.

Invariants (ADR-0113 D1–D3; spec ``design/spec/asset-labels-v0.md``):

* A label resolves to exactly one existing version; labels are scoped per
  asset name.
* ``latest`` is reserved — auto-maintained by the registry, never
  user-settable.
* Tagged union on the freeze record: ``resolved_via == "label"`` requires a
  non-null ``resolved_label``; ``resolved_via == "explicit-version"``
  requires ``resolved_label`` to be null.

Schemas: ``schemas/asset-label-move.schema.json`` and
``schemas/resolved-asset-ref.schema.json`` (Draft 2020-12, version 0.1.0).
"""

from __future__ import annotations

import re
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ASSET_LABELS_SCHEMA_VERSION: Final = "0.1.0"

#: The registry-maintained, read-only-to-the-user label (ADR-0113 D1).
RESERVED_LABEL_LATEST: Final = "latest"

LABEL_RE: Final = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_CONTENT_HASH_RE: Final = re.compile(r"^sha256:[0-9a-f]{64}$")
_ULID_RE: Final = re.compile(r"^[0-7][0-9A-HJKMNP-TV-Z]{25}$")


def validate_label_name(label: str) -> str:
    """Validate a label name against the normative slug rule.

    Mixed-case input is rejected, never silently lowercased (spec §Edge
    cases), so two labels can never merely *look* the same.
    """
    if not LABEL_RE.match(label):
        raise ValueError(
            f"'{label}' is not a valid label "
            "(lowercase: ^[a-z0-9][a-z0-9._-]*$)"
        )
    return label


class AssetLabelMove(BaseModel):
    """One append-only audit row: a deployment label was set or moved.

    Mirrors ``schemas/asset-label-move.schema.json`` (closed schema). The
    current target of a label is the newest row per ``(asset_name, label)``;
    rows are never mutated or deleted (ADR-0113 D2).
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["0.1.0"] = ASSET_LABELS_SCHEMA_VERSION
    label: str
    asset_name: str = Field(min_length=1)
    target_version: str = Field(min_length=1)
    moved_at: str
    moved_by: str = Field(min_length=1)
    previous_version: str | None = None
    asset_type: str | None = None
    content_hash: str | None = None
    reason: str | None = None
    move_id: str | None = None

    @field_validator("label")
    @classmethod
    def _validate_label(cls, v: str) -> str:
        return validate_label_name(v)

    @field_validator("content_hash")
    @classmethod
    def _validate_content_hash(cls, v: str | None) -> str | None:
        if v is not None and not _CONTENT_HASH_RE.match(v):
            raise ValueError(f"'{v}' is not a 'sha256:<64 hex>' content hash")
        return v

    @field_validator("move_id")
    @classmethod
    def _validate_move_id(cls, v: str | None) -> str | None:
        if v is not None and not _ULID_RE.match(v):
            raise ValueError(f"'{v}' is not a ULID")
        return v


class ResolvedAssetRef(BaseModel):
    """The capture-time resolution freeze recorded into a Run Capsule.

    Mirrors ``schemas/resolved-asset-ref.schema.json`` (closed schema). Once
    written it is immutable like the rest of the capsule; replay MUST use
    ``resolved_version`` / ``resolved_content_hash`` and MUST NOT re-resolve
    the label (ADR-0113 D3).
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["0.1.0"] = ASSET_LABELS_SCHEMA_VERSION
    asset_type: str = Field(min_length=1)
    asset_name: str = Field(min_length=1)
    requested_ref: str = Field(min_length=1)
    resolved_via: Literal["label", "explicit-version"]
    resolved_label: str | None
    resolved_version: str = Field(min_length=1)
    resolved_content_hash: str
    resolved_at: str
    label_move_id: str | None = None
    label_moved_at: str | None = None
    registry_uri: str | None = None

    @field_validator("resolved_content_hash")
    @classmethod
    def _validate_resolved_content_hash(cls, v: str) -> str:
        if not _CONTENT_HASH_RE.match(v):
            raise ValueError(f"'{v}' is not a 'sha256:<64 hex>' content hash")
        return v

    @field_validator("label_move_id")
    @classmethod
    def _validate_label_move_id(cls, v: str | None) -> str | None:
        if v is not None and not _ULID_RE.match(v):
            raise ValueError(f"'{v}' is not a ULID")
        return v

    @model_validator(mode="after")
    def _tagged_union(self) -> ResolvedAssetRef:
        """The 0113 tagged-union invariant (strengthened at acceptance)."""
        if self.resolved_via == "label":
            if not self.resolved_label:
                raise ValueError(
                    "resolved_via='label' requires a non-null resolved_label"
                )
        elif self.resolved_label is not None:
            raise ValueError(
                "resolved_via='explicit-version' requires resolved_label=null"
            )
        return self
