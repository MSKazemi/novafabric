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
"""EU AI Act Art. 72 post-market-monitoring (PMM) generator (ADR-0107 §NF-091).

Compiles a post-market-monitoring report from monitoring findings — performance
trends, emerging risks, anomalies — observed over a capsule stream. The load-bearing
design rule (ADR-0107 §NF-091): a finding that crosses the **serious-incident
threshold** does not spin up a parallel deadline mechanism; it produces a *referred*
:class:`~novafabric.compliance.incident.Incident` built from the **shipped ADR-0088
model**, so the existing :class:`~novafabric.compliance.incident.DeadlineClock` governs
its Art. 73 obligations. The generator **reuses** that incident/clock machinery rather
than duplicating it.

Pure-code and offline: no infrastructure, no new dependencies. The generator does not
persist incidents — the caller feeds each referred incident to the existing
``IncidentStore`` / ``DeadlineClock``. This ships NF-091; NF-093/094/097 remain future
design.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field

from novafabric.compliance.incident import Incident, IncidentSeverity

#: Severities at or above which a PMM finding is a *serious incident* and is referred
#: onto the Art. 73 incident clock. Mirrors the Art. 72→Art. 73 escalation path.
PMM_SERIOUS_SEVERITIES: frozenset[IncidentSeverity] = frozenset(
    {IncidentSeverity.CRITICAL, IncidentSeverity.HIGH}
)


class PmmTrend(str, Enum):
    """Direction of a monitored metric over the reporting window."""

    improving = "improving"
    stable = "stable"
    degrading = "degrading"


class PmmFinding(BaseModel):
    """One post-market-monitoring observation."""

    metric: str
    trend: PmmTrend
    severity: IncidentSeverity
    description: str
    run_ids: list[str] = Field(default_factory=list)
    incident_classification: str | None = Field(
        default=None,
        description=(
            "cap-005 incident taxonomy value; REQUIRED for a serious finding so the "
            "referred incident can be classified for the Art. 73 clock"
        ),
    )


class PmmReport(BaseModel):
    """An Art. 72 post-market-monitoring report."""

    system_name: str
    period_start: str
    period_end: str
    findings: list[PmmFinding] = Field(default_factory=list)
    referred_incidents: list[Incident] = Field(default_factory=list)
    generated_at: str


def is_serious(severity: IncidentSeverity) -> bool:
    """Whether a finding of this severity crosses the serious-incident threshold."""
    return severity in PMM_SERIOUS_SEVERITIES


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_pmm_report(
    system_name: str,
    *,
    period_start: str,
    period_end: str,
    findings: Sequence[PmmFinding],
    occurred_at: datetime,
) -> PmmReport:
    """Compile a PMM report, referring serious findings onto the Art. 73 incident clock.

    Every finding is carried in the report. A finding whose severity
    :func:`is_serious` produces a referred :class:`Incident` (from the shipped ADR-0088
    model) anchored at ``occurred_at`` — the caller persists it and lets the existing
    :class:`DeadlineClock` compute its Art. 73 deadlines.

    A serious finding **must** carry an ``incident_classification`` — you cannot open an
    Art. 73 incident without classifying it. A serious finding without one raises
    :class:`ValueError` (fail closed), rather than silently dropping the escalation.
    """
    referred: list[Incident] = []
    for finding in findings:
        if not is_serious(finding.severity):
            continue
        if not finding.incident_classification:
            raise ValueError(
                f"serious PMM finding on '{finding.metric}' "
                f"({finding.severity.value}) requires an incident_classification "
                "to open an Art. 73 incident"
            )
        referred.append(
            Incident(
                title=f"PMM escalation: {finding.metric} {finding.trend.value}",
                classification=finding.incident_classification,
                severity=finding.severity,
                run_ids=list(finding.run_ids),
                occurred_at=occurred_at,
            )
        )
    return PmmReport(
        system_name=system_name,
        period_start=period_start,
        period_end=period_end,
        findings=list(findings),
        referred_incidents=referred,
        generated_at=_now(),
    )
