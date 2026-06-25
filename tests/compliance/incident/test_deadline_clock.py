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
"""Art. 73 DeadlineClock tests (ADR-0088)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from novafabric.compliance.incident.clock import (
    DeadlineClock,
    Obligation,
    applicable_obligations,
)
from novafabric.compliance.incident.models import Incident, IncidentSeverity

OCCURRED = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
AWARE = datetime(2026, 6, 3, 12, 0, tzinfo=timezone.utc)


def _incident(classification: str, aware: datetime | None = AWARE) -> Incident:
    return Incident(
        title="t",
        classification=classification,
        severity=IncidentSeverity.CRITICAL,
        occurred_at=OCCURRED,
        aware_at=aware,
    )


class TestApplicableObligations:
    def test_standard_classification_gets_15_day_only(self) -> None:
        assert applicable_obligations("unauthorized_tool_use") == [
            Obligation.ART_73_2_STANDARD
        ]

    def test_death_adds_10_day(self) -> None:
        obs = applicable_obligations("death_of_person")
        assert Obligation.ART_73_4_DEATH in obs
        assert Obligation.ART_73_2_STANDARD in obs

    def test_widespread_and_critical_infra_add_2_day(self) -> None:
        for cls in ("widespread_infringement", "critical_infrastructure_disruption"):
            assert (
                Obligation.ART_73_3_WIDESPREAD_OR_CRITICAL_INFRA
                in applicable_obligations(cls)
            )

    def test_empty_classification_is_ambiguous_all_emitted(self) -> None:
        assert set(applicable_obligations("  ")) == set(Obligation)


class TestDeadlineClock:
    def test_anchored_to_aware_at_when_set(self) -> None:
        [d] = DeadlineClock.compute(
            _incident("unauthorized_tool_use"), now=AWARE
        )
        assert d.anchor == AWARE
        assert d.deadline == AWARE + timedelta(days=15)
        assert d.days_remaining == 15
        assert not d.overdue

    def test_falls_back_to_occurred_at(self) -> None:
        [d] = DeadlineClock.compute(
            _incident("unauthorized_tool_use", aware=None), now=OCCURRED
        )
        assert d.anchor == OCCURRED

    def test_death_path_10_days(self) -> None:
        deadlines = DeadlineClock.compute(_incident("death_of_person"), now=AWARE)
        by_ob = {d.obligation: d for d in deadlines}
        assert by_ob[Obligation.ART_73_4_DEATH].deadline == AWARE + timedelta(days=10)
        # strictest deadline sorts first
        assert deadlines[0].obligation is Obligation.ART_73_4_DEATH

    def test_widespread_path_2_days(self) -> None:
        deadlines = DeadlineClock.compute(
            _incident("widespread_infringement"), now=AWARE
        )
        by_ob = {d.obligation: d for d in deadlines}
        assert by_ob[
            Obligation.ART_73_3_WIDESPREAD_OR_CRITICAL_INFRA
        ].deadline == AWARE + timedelta(days=2)

    def test_overdue_flips_strictly_after_boundary(self) -> None:
        incident = _incident("unauthorized_tool_use")
        boundary = AWARE + timedelta(days=15)
        [at_boundary] = DeadlineClock.compute(incident, now=boundary)
        assert at_boundary.days_remaining == 0
        assert not at_boundary.overdue
        [after] = DeadlineClock.compute(incident, now=boundary + timedelta(seconds=1))
        assert after.overdue
        assert after.days_remaining == -1

    def test_naive_now_rejected(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            DeadlineClock.compute(
                _incident("unauthorized_tool_use"), now=datetime(2026, 6, 4)
            )
