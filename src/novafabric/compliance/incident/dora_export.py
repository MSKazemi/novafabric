"""EU DORA major-ICT-incident report projection (ADR-0159 D5 / NF-279).

Renders a **DRAFT** EU DORA (Regulation (EU) 2022/2554) major-ICT-related-incident report from a
stored :class:`Incident`, computing the three DORA reporting-stage deadlines from the incident's
anchor timestamp:

* ``initial_notification`` — within **24 hours** of the financial entity becoming aware,
* ``intermediate_report`` — within **72 hours** of the initial notification,
* ``final_report`` — no later than **one month** after the intermediate report.

This is a sibling of the shipped OECD-AIM and NIS2 profiles (:mod:`aim_export`): a pure projection
over data the incident store already holds, reusing the same ``now``-injected deadline convention as
:class:`~novafabric.compliance.incident.clock.DeadlineClock`. It **transmits nothing**
(``transmitted`` is forced ``False``) and carries no compliant/reported/verdict field: deadline
outputs are operational aids, not legal advice, and whether an obligation applies is a legal
determination.

Honesty gap: DORA's tighter *4-hour-from-classification-as-major* bound needs a ``classified_at``
timestamp NovaFabric does not store; that gap is surfaced in ``completeness_summary``, never
fabricated. The 24-hour-from-awareness bound is the outer bound this projection can compute.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pydantic import BaseModel

from novafabric.compliance.export.models import CompletenessSummaryEntry
from novafabric.compliance.export.provenance import EvidenceSource
from novafabric.compliance.incident.models import Incident

_MISSING_REASON = "not_recorded_in_incident_store"
_DISCLAIMER = "Deadline outputs are operational aids, not legal advice (ADR-0088/ADR-0159)."

#: DORA reporting stages as (stage, offset-from-previous, basis).
_INITIAL_HOURS = 24
_INTERMEDIATE_HOURS = 72
_FINAL_DAYS = 30


class DoraDeadline(BaseModel):
    stage: str  # initial_notification | intermediate_report | final_report
    basis: str
    anchor: datetime
    deadline: datetime
    overdue: bool


class DoraIncidentReport(BaseModel):
    report_type: str = "eu-dora-major-ict-incident"
    report_version: str = "0.1"
    incident_id: str
    title: str
    classification: str
    severity: str
    occurred_at: datetime
    aware_at: datetime | None
    linked_run_capsules: list[str]
    deadlines: list[DoraDeadline]
    completeness_summary: list[dict[str, str]]
    transmitted: bool = False  # DRAFT — NovaFabric renders, it never transmits to a regulator
    disclaimer: str = _DISCLAIMER
    generated_at: datetime
    # Intentionally NO compliant/reported/verdict/on_time field — renders evidence, never judges.


def build_dora_report(incident: Incident, *, now: datetime) -> DoraIncidentReport:
    """Render a DRAFT DORA major-ICT-incident report from a stored incident.

    ``now`` is always injected (never a hidden wall clock), consistent with
    :class:`DeadlineClock`. The anchor is the incident's ``aware_at`` (else ``occurred_at``); the
    three stage deadlines chain 24h → +72h → +1 month from it. ``overdue`` is ``now`` past the
    stage deadline. The report transmits nothing and judges nothing.
    """
    stamp = now.astimezone(timezone.utc)
    anchor = incident.aware_at if incident.aware_at is not None else incident.occurred_at

    initial = anchor + timedelta(hours=_INITIAL_HOURS)
    intermediate = initial + timedelta(hours=_INTERMEDIATE_HOURS)
    final = intermediate + timedelta(days=_FINAL_DAYS)
    stages = [
        ("initial_notification", initial,
         "DORA Art. 19 — initial notification no later than 24h after awareness"),
        ("intermediate_report", intermediate,
         "DORA Art. 19 — intermediate report no later than 72h after initial notification"),
        ("final_report", final,
         "DORA Art. 19 — final report no later than one month after the intermediate report"),
    ]
    deadlines = [
        DoraDeadline(stage=stage, basis=basis, anchor=anchor, deadline=due, overdue=stamp > due)
        for stage, due, basis in stages
    ]

    # ADR-0197 I-1: this projection reads only the operator-authored incident
    # store; it verifies no capsule, so every field-group is operator_asserted.
    completeness = [
        CompletenessSummaryEntry(
            field_name=name,
            status="missing",
            reason=_MISSING_REASON,
            evidence_source=EvidenceSource.operator_asserted,
        ).model_dump(mode="json", exclude_none=True)
        for name in ("classified_at", "financial_impact", "affected_clients")
    ]

    return DoraIncidentReport(
        incident_id=incident.id,
        title=incident.title,
        classification=incident.classification,
        severity=incident.severity.value,
        occurred_at=incident.occurred_at,
        aware_at=incident.aware_at,
        linked_run_capsules=list(incident.run_ids),
        deadlines=deadlines,
        completeness_summary=completeness,
        generated_at=stamp,
    )
