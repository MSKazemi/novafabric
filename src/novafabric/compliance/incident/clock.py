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
"""EU AI Act Art. 73 deadline clock (ADR-0088).

Pure functions over an :class:`~novafabric.compliance.incident.models.Incident`
— ``now`` is always injected, never read from a hidden wall clock.

Deadline outputs are operational aids, not legal advice: which obligation
applies to a real incident is a legal determination made from the operator's
declared classification.  When the classification is ambiguous, all candidate
obligations are emitted (fail-informative — the strictest deadline is never
silently dropped).
"""

from __future__ import annotations

import enum
import math
from datetime import datetime, timedelta, timezone

from pydantic import BaseModel

from novafabric.compliance.incident.models import Incident


class Obligation(str, enum.Enum):
    """Art. 73 serious-incident reporting obligation classes."""

    ART_73_2_STANDARD = "art73_2_standard"
    ART_73_3_WIDESPREAD_OR_CRITICAL_INFRA = "art73_3_widespread_or_critical_infra"
    ART_73_4_DEATH = "art73_4_death"


_OBLIGATION_DAYS: dict[Obligation, int] = {
    Obligation.ART_73_2_STANDARD: 15,
    Obligation.ART_73_3_WIDESPREAD_OR_CRITICAL_INFRA: 2,
    Obligation.ART_73_4_DEATH: 10,
}

_OBLIGATION_BASIS: dict[Obligation, str] = {
    Obligation.ART_73_2_STANDARD: (
        "Art. 73(2) — standard serious incident: report no later than 15 days "
        "after awareness"
    ),
    Obligation.ART_73_3_WIDESPREAD_OR_CRITICAL_INFRA: (
        "Art. 73(3) — widespread infringement or serious incident affecting "
        "critical infrastructure: no later than 2 days after awareness"
    ),
    Obligation.ART_73_4_DEATH: (
        "Art. 73(4) — serious incident involving the death of a person: "
        "no later than 10 days after awareness"
    ),
}

#: classification keywords that select additional, stricter obligations
_DEATH_KEYWORDS = ("death",)
_WIDESPREAD_KEYWORDS = ("widespread", "critical_infrastructure")


class ObligationDeadline(BaseModel):
    """One computed reporting deadline for one obligation class."""

    obligation: Obligation
    basis: str
    anchor: datetime
    deadline: datetime
    days_remaining: int
    overdue: bool


def applicable_obligations(classification: str) -> list[Obligation]:
    """Map a declared classification to candidate Art. 73 obligations.

    Keyword-based, deterministic, fail-informative: the 15-day standard
    obligation always applies; ``death`` adds the 10-day path; ``widespread``
    / ``critical_infrastructure`` adds the 2-day path; an empty/blank
    classification is ambiguous and yields all three.
    """
    text = classification.strip().lower()
    if not text:
        return list(Obligation)
    result = [Obligation.ART_73_2_STANDARD]
    if any(k in text for k in _WIDESPREAD_KEYWORDS):
        result.append(Obligation.ART_73_3_WIDESPREAD_OR_CRITICAL_INFRA)
    if any(k in text for k in _DEATH_KEYWORDS):
        result.append(Obligation.ART_73_4_DEATH)
    return result


class DeadlineClock:
    """Computes Art. 73 deadlines from an incident's clock anchor."""

    @staticmethod
    def compute(incident: Incident, now: datetime) -> list[ObligationDeadline]:
        """Return one :class:`ObligationDeadline` per applicable obligation.

        ``overdue`` flips strictly *after* the deadline instant;
        ``days_remaining`` is the signed floor of whole days until it.
        """
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware (UTC)")
        now = now.astimezone(timezone.utc)
        anchor = incident.clock_anchor
        deadlines: list[ObligationDeadline] = []
        for obligation in applicable_obligations(incident.classification):
            deadline = anchor + timedelta(days=_OBLIGATION_DAYS[obligation])
            seconds_left = (deadline - now).total_seconds()
            deadlines.append(
                ObligationDeadline(
                    obligation=obligation,
                    basis=_OBLIGATION_BASIS[obligation],
                    anchor=anchor,
                    deadline=deadline,
                    days_remaining=math.floor(seconds_left / 86400),
                    overdue=now > deadline,
                )
            )
        deadlines.sort(key=lambda d: d.deadline)
        return deadlines
