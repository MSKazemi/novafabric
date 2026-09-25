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

"""Human-override provenance — ADR-0150 D3/P2 (NF-187).

An override records that a human **corrected** the agent — replaced the action
it proposed with a different one — as distinct from *approving* it (the
decision-context receipt, NF-182). It is the raw material for the override
rate an oversight auditor looks at.

Both actions are bound by digest only; the ``reason`` is a machine code or the
digest of a prose reason (D7). Stored as a list at
``facets.conversation.override`` (see the storage deviation in ADR-0150).
Several overrides may anchor to the same turn — a human can correct more than
one proposed action in a single reply.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from novafabric.hitl._records import (
    AccountabilityRecordError,
    LoadedRecords,
    RecordOutcome,
    check_code_or_digest,
    check_digest,
    check_extras,
    check_human_ref,
    check_timestamp,
    check_turn_ref,
    load_records,
    record_fail_open,
)

RECORD_KEY = "override"

_OVERRIDE_FIELDS = frozenset(
    {
        "turn_ref",
        "overridden_action_digest",
        "override_action_digest",
        "overrider",
        "reason",
        "at",
    }
)


class NoOpOverrideError(AccountabilityRecordError):
    """Raised when the override action digest equals the overridden one.

    Replacing an action with itself is not a correction; recording it would
    inflate the override rate with events in which nothing changed.
    """


class OverrideRecord(BaseModel):
    """NF-187: a human replaced the agent's proposed action at ``turn_ref``."""

    model_config = ConfigDict(extra="allow")

    turn_ref: str
    overridden_action_digest: str
    override_action_digest: str
    overrider: str
    reason: str
    at: str

    @model_validator(mode="before")
    @classmethod
    def _check_extras(cls, data: Any) -> Any:
        return check_extras(data, known=_OVERRIDE_FIELDS)

    @field_validator("turn_ref", mode="before")
    @classmethod
    def _check_turn_ref(cls, v: object) -> str:
        return check_turn_ref(v)

    @field_validator("overridden_action_digest", mode="before")
    @classmethod
    def _check_overridden(cls, v: object) -> str:
        return check_digest(v, field_name="overridden_action_digest")

    @field_validator("override_action_digest", mode="before")
    @classmethod
    def _check_override(cls, v: object) -> str:
        return check_digest(v, field_name="override_action_digest")

    @field_validator("overrider", mode="before")
    @classmethod
    def _check_overrider(cls, v: object) -> str:
        return check_human_ref(v, field_name="overrider")

    @field_validator("reason", mode="before")
    @classmethod
    def _check_reason(cls, v: object) -> str:
        return check_code_or_digest(v, field_name="reason")

    @field_validator("at", mode="before")
    @classmethod
    def _check_at(cls, v: object) -> str:
        return check_timestamp(v, field_name="at")

    @model_validator(mode="after")
    def _check_not_noop(self) -> OverrideRecord:
        if self.overridden_action_digest == self.override_action_digest:
            raise NoOpOverrideError(
                "override_action_digest equals overridden_action_digest; an "
                "override must change the action"
            )
        return self


def record_override(
    capsule: dict[str, Any], record: OverrideRecord | Mapping[str, Any]
) -> RecordOutcome:
    """Attach an override to ``facets.conversation.override``; never raises."""
    return record_fail_open(capsule, RECORD_KEY, OverrideRecord, record, unique_per_turn=False)


def load_overrides(capsule: Mapping[str, Any]) -> LoadedRecords[OverrideRecord]:
    """Read every stored override (malformed entries become defects)."""
    return load_records(capsule, RECORD_KEY, OverrideRecord)
