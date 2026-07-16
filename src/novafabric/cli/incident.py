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
"""``nova incident`` — first-class incident record + Art. 73 deadline clock.

Experimental (ADR-0088, gap-010).  Fully offline/local-mode: the record lives
in ``$NOVAFABRIC_HOME/incidents.db``.
"""

from __future__ import annotations

import enum
import json
from datetime import datetime, timezone
from pathlib import Path

import typer

from novafabric.compliance.incident.aim_export import (
    build_aim_report,
    build_nis2_report_from_incident,
)
from novafabric.compliance.incident.clock import DeadlineClock
from novafabric.compliance.incident.dora_export import build_dora_report
from novafabric.compliance.incident.models import (
    Incident,
    IncidentNotFoundError,
    IncidentSeverity,
    IncidentStatus,
)
from novafabric.compliance.incident.store import IncidentStore

incident_app = typer.Typer(
    help=(
        "Record and track AI incidents with EU AI Act Art. 73 deadline "
        "awareness (experimental). Deadline outputs are operational aids, "
        "not legal advice."
    ),
    no_args_is_help=True,
)

_DISCLAIMER = "Note: deadlines are operational aids, not legal advice (ADR-0088)."


class ExportFormat(str, enum.Enum):
    """Incident report export targets."""

    AIM = "aim"
    NIS2 = "nis2"
    DORA = "dora"


def _parse_ts(value: str, param_name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise typer.BadParameter(
            f"{param_name} must be ISO 8601 (e.g. 2026-06-12T10:00:00+00:00)"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


@incident_app.command("open")
def incident_open_cmd(
    title: str = typer.Option(..., "--title", help="Short human description"),
    classification: str = typer.Option(
        ...,
        "--classification",
        help=(
            "Incident taxonomy value (cap-005 vocabulary); keywords 'death', "
            "'widespread', 'critical_infrastructure' select stricter Art. 73 "
            "deadlines"
        ),
    ),
    severity: IncidentSeverity = typer.Option(
        ..., "--severity", help="critical|high|medium|low"
    ),
    occurred_at: str = typer.Option(
        ..., "--occurred-at", help="ISO 8601 UTC timestamp of the incident event"
    ),
    aware_at: str | None = typer.Option(
        None,
        "--aware-at",
        help="ISO 8601 UTC timestamp of operator awareness (Art. 73 clock anchor)",
    ),
    run_ids: list[str] = typer.Option(
        [], "--run-id", help="Linked run-capsule ID (repeatable)"
    ),
) -> None:
    """Open a new incident record.

    Scope: local incident store under NOVAFABRIC_HOME.

    \b
    Examples:
      nova incident open --title "Tool misuse in prod agent" \\
        --classification unauthorized_tool_use --severity high \\
        --occurred-at 2026-06-10T08:00:00+00:00 --run-id run-abc123
    """
    try:
        incident = Incident(
            title=title,
            classification=classification,
            severity=severity,
            occurred_at=_parse_ts(occurred_at, "--occurred-at"),
            aware_at=_parse_ts(aware_at, "--aware-at") if aware_at else None,
            run_ids=list(run_ids),
        )
    except ValueError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    with IncidentStore() as store:
        store.create(incident)
    typer.echo(f"Opened incident {incident.id} (status={incident.status.value})")
    typer.echo(_DISCLAIMER)


@incident_app.command("list")
def incident_list_cmd() -> None:
    """List incidents with status and most-pressing deadline.

    Scope: local incident store under NOVAFABRIC_HOME.

    \b
    Examples:
      nova incident list
    """
    now = datetime.now(timezone.utc)
    with IncidentStore() as store:
        incidents = store.list_all()
    if not incidents:
        typer.echo("No incidents recorded.")
        return
    for inc in incidents:
        pressing = DeadlineClock.compute(inc, now=now)[0]
        flag = "OVERDUE" if pressing.overdue else f"{pressing.days_remaining}d left"
        typer.echo(
            f"{inc.id}  [{inc.status.value:8}]  {inc.severity.value:8}  "
            f"{flag:>10}  {inc.title}"
        )
    typer.echo(_DISCLAIMER)


@incident_app.command("status")
def incident_status_cmd(
    incident_id: str = typer.Argument(..., help="Incident identifier"),
) -> None:
    """Show the full incident record and its Art. 73 deadline table.

    Scope: local incident store under NOVAFABRIC_HOME.

    \b
    Examples:
      nova incident status inc-0123abcd4567
    """
    now = datetime.now(timezone.utc)
    with IncidentStore() as store:
        try:
            incident = store.get(incident_id)
        except IncidentNotFoundError as exc:
            typer.echo(f"Error: incident not found: {incident_id}", err=True)
            raise typer.Exit(1) from exc
    typer.echo(f"id:             {incident.id}")
    typer.echo(f"title:          {incident.title}")
    typer.echo(f"classification: {incident.classification}")
    typer.echo(f"severity:       {incident.severity.value}")
    typer.echo(f"status:         {incident.status.value}")
    typer.echo(f"occurred_at:    {incident.occurred_at.isoformat()}")
    aware_text = (
        incident.aware_at.isoformat()
        if incident.aware_at
        else "(unset — clock falls back to occurred_at)"
    )
    typer.echo(f"aware_at:       {aware_text}")
    typer.echo(f"run_ids:        {', '.join(incident.run_ids) or '(none)'}")
    typer.echo(f"attestation:    {incident.attestation_ref or '(none)'}")
    typer.echo("")
    typer.echo("Art. 73 reporting deadlines:")
    for d in DeadlineClock.compute(incident, now=now):
        state = "OVERDUE" if d.overdue else f"{d.days_remaining} day(s) remaining"
        typer.echo(f"  {d.obligation.value:40} {d.deadline.isoformat()}  {state}")
        typer.echo(f"      basis: {d.basis}")
    typer.echo(_DISCLAIMER)


@incident_app.command("export")
def incident_export_cmd(
    incident_id: str = typer.Argument(..., help="Incident identifier"),
    format_: ExportFormat = typer.Option(
        ExportFormat.AIM, "--format", help="Report target: aim|nis2|dora"
    ),
    output: Path | None = typer.Option(
        None, "--output", help="Output path (default: incident-<id>-<format>.json)"
    ),
) -> None:
    """Render an incident report (OECD AIM, NIS2, or EU DORA) from the stored record.

    Writing an `aim` or `nis2` export transitions an `open` incident to `reported`. The `dora`
    format is a **DRAFT projection** (``transmitted: false``, ADR-0159 D5) and does **not**
    transition the incident — it transmits nothing.

    Scope: local incident store under NOVAFABRIC_HOME; writes one JSON file.

    \b
    Examples:
      nova incident export inc-0123abcd4567 --format aim
      nova incident export inc-0123abcd4567 --format nis2 --output report.json
      nova incident export inc-0123abcd4567 --format dora
    """
    with IncidentStore() as store:
        try:
            incident = store.get(incident_id)
        except IncidentNotFoundError as exc:
            typer.echo(f"Error: incident not found: {incident_id}", err=True)
            raise typer.Exit(1) from exc
        if format_ is ExportFormat.AIM:
            payload = build_aim_report(incident)
        elif format_ is ExportFormat.DORA:
            payload = build_dora_report(
                incident, now=datetime.now(timezone.utc)
            ).model_dump(mode="json")
        else:
            payload = build_nis2_report_from_incident(incident).model_dump()
        path = output or Path(f"incident-{incident.id}-{format_.value}.json")
        path.write_text(json.dumps(payload, indent=2, default=str) + "\n")
        # DORA is a non-transmitted DRAFT — never transition the incident on it.
        if format_ is not ExportFormat.DORA and incident.status is IncidentStatus.OPEN:
            store.transition(incident.id, IncidentStatus.REPORTED)
            typer.echo(f"Incident {incident.id}: open -> reported")
    typer.echo(f"Wrote {format_.value} report to {path}")
    typer.echo(_DISCLAIMER)
