from __future__ import annotations

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from novafabric.retention.holds import (
    HoldNotFoundError,
    InvalidRegistryNameError,
    active_holds,
    place_hold,
    release_hold,
)

console = Console()
app = typer.Typer(
    help="Place and release legal holds to prevent capsule deletion.",
    no_args_is_help=True,
)

# Holds live in ONE store shared with the `/api/holds` routes:
# $NOVAFABRIC_HOME/registries/<registry>/holds.jsonl (novafabric.retention.holds).
# Holds written before that store existed (./.novafabric/registries/<registry>/ under
# the working directory, or <capsule_dir>/../registries/ by `nova serve`) are still
# read by every deletion gate and can still be released here.


@app.command("create")
def hold_create(
    registry: str = typer.Argument(..., help="Registry name"),
    reason: str = typer.Option(..., "--reason", help="Reason for the legal hold"),
    duration_days: int | None = typer.Option(
        None, "--duration-days", help="Hold duration in days (omit for indefinite)"
    ),
) -> None:
    """Place a legal hold on a registry to prevent capsule deletion.

    A legal hold blocks all capsule deletions in the named registry until
    released. Use for litigation holds, investigations, or compliance audits.
    The hold is written to $NOVAFABRIC_HOME/registries/<registry>/holds.jsonl,
    the same store `POST /api/holds` writes, so `nova retention`, `nova capsule
    delete` and the server delete routes all honour it whichever surface placed it.

    Scope: single registry.

    \b
    Examples:
      nova hold create my-registry --reason "Litigation hold for case #42"
      nova hold create my-registry --reason "Audit" --duration-days 90
    """
    try:
        record = place_hold(registry, reason, duration_days, actor="cli-user")
    except (InvalidRegistryNameError, ValueError) as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(code=1) from None
    console.print(
        f"[green]Hold created:[/green] {record.hold_id} on registry '{escape(registry)}'"
    )
    if duration_days is None:
        console.print("  Duration: indefinite")
    else:
        console.print(f"  Duration: {duration_days} days")


@app.command("list")
def hold_list(
    registry: str = typer.Argument(..., help="Registry name"),
) -> None:
    """List all active legal holds for a registry.

    Includes holds placed through the API and holds still stored at a
    pre-unification location. An unreadable ledger line is listed as an active
    `__corrupt__:<registry>` hold (fail closed).

    Scope: single registry.

    \b
    Examples:
      nova hold list my-registry
    """
    try:
        holds = active_holds(registry)
    except InvalidRegistryNameError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(code=1) from None
    if not holds:
        console.print("No active holds.")
        return
    table = Table("Hold ID", "Reason", "Duration", "Created")
    for h in holds:
        duration = f"{h.duration_days}d" if h.duration_days is not None else "indefinite"
        table.add_row(h.hold_id, escape(h.reason), duration, h.created_at[:19])
    console.print(table)


@app.command("release")
def hold_release(
    hold_id: str = typer.Argument(..., help="Hold ID to release"),
) -> None:
    """Release a legal hold, allowing capsule deletions to resume.

    Finds the hold wherever it is stored — including holds placed through
    `POST /api/holds` and holds at a pre-unification location.

    Scope: single hold (searched across all registries).

    \b
    Examples:
      nova hold release <hold-id>
    """
    try:
        release_hold(hold_id, actor="cli-user")
    except (InvalidRegistryNameError, HoldNotFoundError) as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(code=1) from None
    console.print(f"[green]Released hold:[/green] {hold_id}")
