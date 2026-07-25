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
"""ADR-0107 §NF-091 — Art. 72 post-market-monitoring generator.

The generator compiles a PMM report from monitoring findings, and — the load-bearing
integration — a finding that crosses the serious-incident threshold produces a
*referred* :class:`Incident` built from the shipped ADR-0088 model, so the existing
:class:`DeadlineClock` governs its Art. 73 deadlines. It reuses that machinery rather
than duplicating a second clock.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from novafabric.compliance.export.pmm import (
    PmmFinding,
    PmmTrend,
    build_pmm_report,
    is_serious,
)
from novafabric.compliance.incident import DeadlineClock, Incident, IncidentSeverity

_OCCURRED = datetime(2026, 7, 1, tzinfo=timezone.utc)


def _finding(severity: IncidentSeverity, **kw: object) -> PmmFinding:
    base: dict[str, object] = {
        "metric": "hallucination_rate",
        "trend": PmmTrend.degrading,
        "severity": severity,
        "description": "rising over the monitoring window",
    }
    base.update(kw)
    return PmmFinding(**base)  # type: ignore[arg-type]


class TestPmmReport:
    def test_report_carries_all_findings(self) -> None:
        report = build_pmm_report(
            system_name="triage-agent",
            period_start="2026-06-01",
            period_end="2026-07-01",
            findings=[_finding(IncidentSeverity.LOW), _finding(IncidentSeverity.MEDIUM)],
            occurred_at=_OCCURRED,
        )
        assert report.system_name == "triage-agent"
        assert len(report.findings) == 2
        assert report.referred_incidents == []

    def test_serious_finding_refers_an_incident(self) -> None:
        report = build_pmm_report(
            system_name="triage-agent",
            period_start="2026-06-01",
            period_end="2026-07-01",
            findings=[
                _finding(IncidentSeverity.HIGH, incident_classification="widespread_infringement"),
            ],
            occurred_at=_OCCURRED,
        )
        assert len(report.referred_incidents) == 1
        inc = report.referred_incidents[0]
        assert isinstance(inc, Incident)
        assert inc.severity is IncidentSeverity.HIGH
        assert inc.classification == "widespread_infringement"
        assert inc.occurred_at == _OCCURRED

    def test_non_serious_findings_do_not_refer(self) -> None:
        report = build_pmm_report(
            system_name="s",
            period_start="2026-06-01",
            period_end="2026-07-01",
            findings=[_finding(IncidentSeverity.LOW), _finding(IncidentSeverity.MEDIUM)],
            occurred_at=_OCCURRED,
        )
        assert report.referred_incidents == []

    def test_serious_finding_without_classification_is_rejected(self) -> None:
        # You cannot open an Art. 73 incident without classifying it — fail closed.
        with pytest.raises(ValueError, match="classification"):
            build_pmm_report(
                system_name="s",
                period_start="2026-06-01",
                period_end="2026-07-01",
                findings=[_finding(IncidentSeverity.CRITICAL)],
                occurred_at=_OCCURRED,
            )

    def test_is_serious_threshold(self) -> None:
        assert is_serious(IncidentSeverity.CRITICAL) is True
        assert is_serious(IncidentSeverity.HIGH) is True
        assert is_serious(IncidentSeverity.MEDIUM) is False
        assert is_serious(IncidentSeverity.LOW) is False


class TestReferredIncidentFeedsExistingClock:
    """The integration proof: a referred incident drives the shipped DeadlineClock."""

    def test_referred_incident_produces_art73_deadlines(self) -> None:
        report = build_pmm_report(
            system_name="triage-agent",
            period_start="2026-06-01",
            period_end="2026-07-01",
            findings=[
                _finding(
                    IncidentSeverity.CRITICAL,
                    incident_classification="critical_infrastructure_disruption",
                ),
            ],
            occurred_at=_OCCURRED,
        )
        inc = report.referred_incidents[0]
        # No duplicated clock — the existing DeadlineClock consumes the referred incident.
        now = datetime(2026, 7, 2, tzinfo=timezone.utc)
        deadlines = DeadlineClock.compute(inc, now)
        assert deadlines  # at least the 15-day standard obligation
        assert all(d.anchor == _OCCURRED for d in deadlines)
        # critical_infrastructure classification adds the 2-day widespread/CI path.
        assert any(d.days_remaining <= 2 for d in deadlines)
