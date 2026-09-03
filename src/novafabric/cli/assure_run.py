"""nova assure-run — record and check continuous-assurance runs (ADR-0147 D7, NF-159).

``record`` writes the attestation that a scheduled assurance run executed.
``check`` reports whether the next run is overdue — which is how a run that
*never happened* is detected: it left no record, so the verdict comes from the
previous run's ``next_due``.

``check`` exits ``1`` when a run is overdue. That is a genuine failed check, not a
detector observation, so unlike ``nova drift`` it is a non-zero exit.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console

from novafabric.assure._honesty import HONESTY_LINE
from novafabric.assure.attestation import (
    AssuranceAttestation,
    AttestationError,
    check_overdue,
    facet_from_capsule,
    record_run,
)

app = typer.Typer(
    name="assure-run",
    help="Record and check continuous-assurance runs (experimental, ADR-0147 NF-159).",
    no_args_is_help=True,
)

console = Console()
err_console = Console(stderr=True)


@app.command("record")
def record(
    schedule_id: Annotated[
        str, typer.Option("--schedule", help="Identifier of the assurance schedule.")
    ],
    ran_at: Annotated[
        str, typer.Option("--ran-at", help="RFC 3339 timestamp of the run.")
    ],
    cadence_seconds: Annotated[
        int, typer.Option("--cadence", help="Expected seconds between runs.")
    ],
    baseline: Annotated[
        list[str] | None,
        typer.Option("--baseline", help="A baseline_id checked (repeatable)."),
    ] = None,
    detector: Annotated[
        list[str] | None,
        typer.Option("--detector", help="A detector that ran (repeatable)."),
    ] = None,
    alarms: Annotated[
        int, typer.Option("--alarms", help="How many alarms fired.")
    ] = 0,
    out: Annotated[
        Path | None, typer.Option("--out", help="Write the attestation JSON here.")
    ] = None,
) -> None:
    """Record that a scheduled assurance run executed."""
    try:
        attestation = record_run(
            schedule_id,
            ran_at=ran_at,
            cadence_seconds=cadence_seconds,
            baselines_checked=list(baseline or []),
            detectors_run=list(detector or []),
            alarms_fired=alarms,
        )
    except AttestationError as exc:
        err_console.print(f"[red]Cannot record the run:[/red] {exc}")
        raise typer.Exit(2) from exc

    payload = attestation.model_dump()
    if out is not None:
        out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        console.print(
            f"[green]Recorded[/green] {schedule_id}; next due {attestation.next_due}"
        )
    else:
        console.print_json(json.dumps(payload))
    console.print(f"[dim]{HONESTY_LINE}[/dim]")


@app.command("check")
def check(
    attestation_file: Annotated[
        Path,
        typer.Option("--attestation", help="Attestation JSON, or a capsule with one."),
    ],
    now: Annotated[
        str, typer.Option("--now", help="RFC 3339 instant to judge against.")
    ],
) -> None:
    """Report whether the next assurance run is overdue."""
    if not attestation_file.is_file():
        err_console.print(f"[red]Attestation not found:[/red] {attestation_file}")
        raise typer.Exit(2)
    try:
        doc: Any = json.loads(attestation_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        err_console.print(f"[red]Could not read the attestation:[/red] {exc}")
        raise typer.Exit(2) from exc
    if not isinstance(doc, dict):
        err_console.print("[red]Attestation must be a JSON object.[/red]")
        raise typer.Exit(2)

    try:
        attestation = facet_from_capsule(doc)
        if attestation is None:
            attestation = AssuranceAttestation.model_validate(doc)
        verdict = check_overdue(attestation, now=now)
    except (AttestationError, ValueError) as exc:
        err_console.print(f"[red]Invalid attestation:[/red] {exc}")
        raise typer.Exit(2) from exc

    console.print_json(json.dumps(verdict.model_dump()))
    console.print(f"[dim]{HONESTY_LINE}[/dim]")
    if verdict.overdue:
        err_console.print(
            f"[red]overdue:[/red] {verdict.schedule_id} was due "
            f"{verdict.next_due}, {verdict.late_by_seconds}s ago"
        )
        raise typer.Exit(1)
