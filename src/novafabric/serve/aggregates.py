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
"""The honest-degradation rule for aggregates — ADR-0234 D2/D3.

    An aggregate that cannot be computed faithfully under the active view state
    must refuse to render, and must say why. It may not render an approximation,
    a partial result, or a zero, in place of an unavailable one.

D2 governs **every** aggregate — the strip, charts, dashboard widgets, report
exports, any KPI tile — which is why the rule lives here once rather than being
re-derived per panel. ADR-0234's own Consequences put it plainly: *"inconsistent
honesty is indistinguishable from dishonesty from the user's side."*

## Why refusing beats a warning badge

The conventional choice is to render the approximation with a badge. ADR-0234
rejects it, and the reasoning is worth keeping next to the code: **a number on
screen is read as a number regardless of the badge beside it.** A warning that
competes with a rendered figure loses. Refusal is the only mechanism that
actually prevents the false conclusion.

## "Absent is not zero" is the load-bearing half

`run-capsule.schema.json` states it as an invariant of the format — per-usage
roll-ups are a *"pure sum; absent per-call fields are skipped, never counted as
0"* — and a null cost means **unpriced**, not free. An aggregate that coerces
absent to zero contradicts the schema's own stated semantics.

⚠ **The converse is not true, and conflating them would be its own defect.** A
count of nothing is legitimately `0`: "no runs failed in this bucket" is a fact,
not a missing measurement. This module is about *unmeasured* quantities, never
about measured zeroes. :class:`AggregateCondition` has no member for "the number
was small".
"""

from __future__ import annotations

import enum
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Final

__all__ = [
    "AggregateCondition",
    "AggregateVerdict",
    "computable",
    "refuse",
    "worst",
]


class AggregateCondition(str, enum.Enum):
    """Why an aggregate cannot be computed faithfully (ADR-0234 D2).

    Exactly the four the ADR enumerates. A fifth would mean the rule grew a case
    nobody decided, so a new one belongs in an ADR amendment, not here.
    """

    #: A filter could not be pushed into the aggregation, so the aggregate would
    #: summarize a different population than the table beneath it.
    UNPUSHABLE_FILTER = "unpushable_filter"
    #: The underlying set is bounded by ADR-0199. A total over a truncated set
    #: is not a total.
    TRUNCATED_SOURCE = "truncated_source"
    #: A contributing field is legitimately absent — an unpriced call, a usage
    #: component the provider never reported, an unverifiable trust claim.
    ABSENT_CONTRIBUTOR = "absent_contributor"
    #: ADR-0229 declares a required store tenant-unsafe, so the question cannot
    #: be answered for one tenant at all.
    TENANT_UNSAFE_STORE = "tenant_unsafe_store"
    #: The backing store is not configured or not reachable. Distinct from
    #: ABSENT_CONTRIBUTOR: there the data exists and part of it is missing;
    #: here nothing can be asked.
    SOURCE_UNAVAILABLE = "source_unavailable"


@dataclass(frozen=True)
class AggregateVerdict:
    """Either a faithful value, or a refusal that says why and what would fix it.

    A refusal deliberately carries **no** ``value``. Returning ``0`` or ``None``
    alongside ``computable=False`` would put a number on the wire for a caller to
    render, and the whole rule exists because a rendered number is believed.
    """

    computable: bool
    value: Any = None
    condition: AggregateCondition | None = None
    reason: str = ""
    #: D3 — what would make it computable. *"Cannot chart cost with an
    #: unpushable metadata filter — remove it, or switch the metric to count"*
    #: is actionable; *"Unavailable"* is not. It is the difference between a
    #: rule that teaches the data model and one that feels like an obstruction.
    remedy: str = ""
    #: Non-fatal context a caller may surface alongside a computable value —
    #: e.g. how many calls were unpriced. Never a substitute for a refusal.
    notes: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """Wire form. A refusal omits ``value`` entirely rather than nulling it."""
        payload: dict[str, Any] = {"computable": self.computable}
        if self.computable:
            payload["value"] = self.value
        else:
            payload["condition"] = self.condition.value if self.condition else None
            payload["reason"] = self.reason
            payload["remedy"] = self.remedy
        if self.notes:
            payload["notes"] = dict(self.notes)
        return payload


def computable(value: Any, **notes: Any) -> AggregateVerdict:
    """A faithful aggregate, optionally carrying non-fatal context."""
    return AggregateVerdict(computable=True, value=value, notes=dict(notes))


def refuse(
    condition: AggregateCondition, *, reason: str, remedy: str, **notes: Any
) -> AggregateVerdict:
    """Refuse to produce an aggregate, stating why and what would fix it.

    Both *reason* and *remedy* are required. ADR-0234's Negative consequences
    name a bad refusal message as the entire risk of this design — users will
    meet refusals and some will read them as the product being broken — so a
    refusal with nothing actionable in it is worse than the approximation it
    replaced.
    """
    if not reason.strip():
        raise ValueError("a refusal must state a reason (ADR-0234 D3)")
    if not remedy.strip():
        raise ValueError(
            "a refusal must state what would make it computable (ADR-0234 D3); "
            "'unavailable' is not a remedy"
        )
    return AggregateVerdict(
        computable=False,
        condition=condition,
        reason=reason,
        remedy=remedy,
        notes=dict(notes),
    )


#: Refusal beats computability, and among refusals the earliest listed wins.
#: Ordered most- to least-fundamental: a store you cannot reach makes every
#: other question moot, and a tenancy refusal outranks a data-shape one because
#: it is an access decision rather than a completeness one.
_SEVERITY: Final[tuple[AggregateCondition, ...]] = (
    AggregateCondition.SOURCE_UNAVAILABLE,
    AggregateCondition.TENANT_UNSAFE_STORE,
    AggregateCondition.TRUNCATED_SOURCE,
    AggregateCondition.UNPUSHABLE_FILTER,
    AggregateCondition.ABSENT_CONTRIBUTOR,
)


def worst(verdicts: Sequence[AggregateVerdict]) -> AggregateVerdict:
    """Combine verdicts: any refusal refuses, and the most fundamental one is reported.

    A panel usually asks several questions at once. Reporting the *first*
    refusal encountered would make the message depend on evaluation order, and
    an operator who fixes the reported cause only to meet a second one has been
    given a worse experience than a single accurate answer.

    An empty sequence is computable with no value — there was nothing to refuse.
    """
    refusals = [v for v in verdicts if not v.computable]
    if not refusals:
        merged: dict[str, Any] = {}
        for verdict in verdicts:
            merged.update(verdict.notes)
        return AggregateVerdict(
            computable=True,
            value=[v.value for v in verdicts] if verdicts else None,
            notes=merged,
        )
    ranked = sorted(
        refusals,
        key=lambda v: _SEVERITY.index(v.condition) if v.condition in _SEVERITY else len(_SEVERITY),
    )
    return ranked[0]
