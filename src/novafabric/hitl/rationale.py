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

"""Agent rationale surfaced to the human — ADR-0150 D3/P2 (NF-188).

Records that, at ``turn_ref``, the agent surfaced a stated reason to the human,
bound by ``stated_reason_digest``, together with the ``model_ref`` that
produced it. Sealed with the capsule, this makes "the agent explained X and the
human decided anyway" provable without the capsule holding X.

Record-only (I-4): NovaFabric records the stated reason; it does not judge
whether the reason is true or faithful (that is claim grounding, NF-099, out of
scope). Stored as a list at ``facets.conversation.rationale``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from novafabric.hitl._records import (
    LoadedRecords,
    RecordContentError,
    RecordOutcome,
    check_digest,
    check_extras,
    check_timestamp,
    check_turn_ref,
    load_records,
    record_fail_open,
)

RECORD_KEY = "rationale"

#: A model identifier such as ``anthropic/claude-x`` or ``gpt-4o@2024-08-06``:
#: no whitespace (so no prose), bounded length.
_MODEL_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,255}\Z")

_RATIONALE_FIELDS = frozenset({"turn_ref", "stated_reason_digest", "surfaced_at", "model_ref"})


class RationaleRecord(BaseModel):
    """NF-188: the agent's stated reason, surfaced to the human at a turn."""

    model_config = ConfigDict(extra="allow")

    turn_ref: str
    stated_reason_digest: str
    surfaced_at: str
    model_ref: str

    @model_validator(mode="before")
    @classmethod
    def _check_extras(cls, data: Any) -> Any:
        return check_extras(data, known=_RATIONALE_FIELDS)

    @field_validator("turn_ref", mode="before")
    @classmethod
    def _check_turn_ref(cls, v: object) -> str:
        return check_turn_ref(v)

    @field_validator("stated_reason_digest", mode="before")
    @classmethod
    def _check_reason(cls, v: object) -> str:
        return check_digest(v, field_name="stated_reason_digest")

    @field_validator("surfaced_at", mode="before")
    @classmethod
    def _check_surfaced_at(cls, v: object) -> str:
        return check_timestamp(v, field_name="surfaced_at")

    @field_validator("model_ref", mode="before")
    @classmethod
    def _check_model_ref(cls, v: object) -> str:
        if not isinstance(v, str) or not _MODEL_REF_RE.fullmatch(v):
            raise RecordContentError(
                "model_ref must be a model identifier (no whitespace, <= 256 chars)"
            )
        return v


def record_rationale(
    capsule: dict[str, Any], record: RationaleRecord | Mapping[str, Any]
) -> RecordOutcome:
    """Attach a rationale to ``facets.conversation.rationale``; never raises."""
    return record_fail_open(capsule, RECORD_KEY, RationaleRecord, record, unique_per_turn=False)


def load_rationales(capsule: Mapping[str, Any]) -> LoadedRecords[RationaleRecord]:
    """Read every stored rationale (malformed entries become defects)."""
    return load_records(capsule, RECORD_KEY, RationaleRecord)
