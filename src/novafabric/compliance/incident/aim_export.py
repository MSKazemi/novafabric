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
"""Incident report exporters (ADR-0088).

Two render targets from one stored :class:`Incident` record:

* :func:`build_aim_report` — OECD AI Incidents Monitor (AIM) JSON.  The AIM
  schema is consumed best-effort (documented mapping); fields NovaFabric
  cannot populate are listed in ``completeness_summary`` with a reason, per
  the cap-005 convention.
* :func:`build_nis2_report_from_incident` — renders the existing cap-005
  :class:`NIS2IncidentReport` from a stored incident, without touching the
  exporter's original call path.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from novafabric.compliance.export.models import (
    CompletenessSummaryEntry,
    NIS2IncidentReport,
)
from novafabric.compliance.export.provenance import EvidenceSource
from novafabric.compliance.incident.clock import DeadlineClock
from novafabric.compliance.incident.models import Incident

_AIM_MISSING_REASON = "not_recorded_in_incident_store"


def build_aim_report(
    incident: Incident, now: datetime | None = None
) -> dict[str, Any]:
    """Render an OECD AIM-style incident report as a JSON-serialisable dict.

    Best-effort mapping onto the AIM structured-report fields ([EVIDENCE
    src-804] in gap-010); unpopulated fields are explicit, never silently
    omitted.
    """
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    # ADR-0197 I-1: AIM report is a projection over operator-authored incident
    # data; NovaFabric verifies no capsule here, so mark operator_asserted.
    completeness = [
        CompletenessSummaryEntry(
            field_name=name,
            status="missing",
            reason=_AIM_MISSING_REASON,
            evidence_source=EvidenceSource.operator_asserted,
        ).model_dump(mode="json", exclude_none=True)
        for name in ("harmed_parties", "economic_sector", "media_references")
    ]
    deadlines = DeadlineClock.compute(incident, now=stamp)
    return {
        "report_type": "oecd-aim",
        "report_version": "0.1",
        "incident_id": incident.id,
        "title": incident.title,
        "classification": incident.classification,
        "severity": incident.severity.value,
        "status": incident.status.value,
        "occurred_at": incident.occurred_at.isoformat(),
        "aware_at": (
            incident.aware_at.isoformat() if incident.aware_at else None
        ),
        "linked_run_capsules": list(incident.run_ids),
        "reperformance_attestation_ref": incident.attestation_ref,
        "art73_deadlines": [d.model_dump(mode="json") for d in deadlines],
        "completeness_summary": completeness,
        "disclaimer": (
            "Deadline outputs are operational aids, not legal advice "
            "(ADR-0088)"
        ),
        "generated_at": stamp.isoformat(),
    }


def build_nis2_report_from_incident(
    incident: Incident, now: datetime | None = None
) -> NIS2IncidentReport:
    """Map a stored incident onto the cap-005 NIS2 report model.

    Phase 2/3 narrative fields stay unset (they are authored at report time,
    not stored on the record); the completeness summary says so explicitly.
    """
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    completeness = [
        CompletenessSummaryEntry(
            field_name=name,
            status="missing",
            reason="authored_at_report_time_not_stored_on_incident_record",
            evidence_source=EvidenceSource.operator_asserted,
        )
        for name in (
            "initial_root_cause_path",
            "initial_scope_estimate",
            "actions_taken",
            "final_root_cause",
            "cross_border_impact_assessment",
            "lessons_learned",
        )
    ]
    return NIS2IncidentReport(
        incident_id=incident.id,
        report_phase=1,
        first_detection_timestamp=incident.clock_anchor.isoformat(),
        preliminary_classification=incident.classification,
        preliminary_severity_indicator=incident.severity.value,
        cross_border_dimensions_flag=False,
        affected_systems_list=list(incident.run_ids) or None,
        completeness_summary=completeness,
        generated_at=stamp.isoformat(),
    )
