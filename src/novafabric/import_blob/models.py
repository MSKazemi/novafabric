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

"""Typed models for the import receipt (ADR-0207 D7, spec batch-import-v0).

The receipt is a sidecar report (like the export verifier's), **not** a third
top-level format. Additive-only evolution; ``schema_version`` starts at
``"0.1.0"``.
"""

from __future__ import annotations

from typing import Any, Final, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field

#: Frozen receipt schema version (spec batch-import-v0 §Import report).
RECEIPT_SCHEMA_VERSION: Final = "0.1.0"

#: Member actions (spec batch-import-v0 §Import report — member record).
MemberAction = Literal[
    "imported", "skipped_existing", "collision", "failed", "not_processed"
]


class CollisionDetail(BaseModel):
    """Both hashes of a same-``run_id``-different-content collision (D5)."""

    model_config = ConfigDict(extra="forbid")

    local_hash: str
    manifest_hash: str


class MemberRecord(BaseModel):
    """One per manifest member: what happened to it in this import run."""

    model_config = ConfigDict(extra="forbid")

    capsule_id: str = Field(min_length=1)
    content_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    action: MemberAction
    detail: Optional[Union[str, CollisionDetail]] = None


class VerificationInfo(BaseModel):
    """How (and whether) the batch verified before any store write (D2)."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["signed", "unsigned"]
    status: Literal["VALID", "INVALID", "INCOMPLETE"]
    problems: list[str] = Field(default_factory=list)


class ImportCounts(BaseModel):
    """Per-action totals; sum to ``len(members)`` once classification ran."""

    model_config = ConfigDict(extra="forbid")

    imported: int = 0
    skipped_existing: int = 0
    collisions: int = 0
    failed: int = 0


class ReindexInfo(BaseModel):
    """Derived-index outcomes (D6); all zero/empty for --no-reindex/--dry-run."""

    model_config = ConfigDict(extra="forbid")

    lineage_capsules: int = 0
    runs_cache_rows: int = 0
    errors: list[str] = Field(default_factory=list)


class ImportProducer(BaseModel):
    """The importer's identity, for forward-compat."""

    model_config = ConfigDict(extra="forbid")

    tool: Literal["novafabric"] = "novafabric"
    version: str = Field(min_length=1)


class ImportReceipt(BaseModel):
    """The import receipt — every run leaves evidence, refusals included (D7)."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["0.1.0"] = RECEIPT_SCHEMA_VERSION
    import_id: str = Field(pattern=r"^[0-7][0-9A-HJKMNP-TV-Z]{25}$")
    dry_run: bool = False
    source: str = Field(min_length=1)
    export_id: Optional[str] = None
    batch_digest: Optional[str] = None
    verification: VerificationInfo
    members: list[MemberRecord] = Field(default_factory=list)
    counts: ImportCounts = Field(default_factory=ImportCounts)
    reindex: ReindexInfo = Field(default_factory=ReindexInfo)
    started_at: str
    finished_at: str
    producer: ImportProducer
    extensions: Optional[dict[str, Any]] = None

    def to_json_dict(self) -> dict[str, Any]:
        """JSON-ready dict; drops ``extensions`` and member ``detail`` when unset."""
        data = self.model_dump(mode="json")
        if data.get("extensions") is None:
            data.pop("extensions", None)
        for member in data.get("members", []):
            if member.get("detail") is None:
                member.pop("detail", None)
        return data
