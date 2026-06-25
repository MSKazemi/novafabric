from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from novafabric.audit import AUDIT_LOG_PATH, AuditEventType, AuditLog

console = Console()
app = typer.Typer(
    help="Place and release legal holds to prevent capsule deletion.",
    no_args_is_help=True,
)

# Holds are stored as append-only JSONL in .novafabric/registries/<registry>/holds.jsonl
# Each record: {"hold_id": str, "registry": str, "reason": str,
#               "duration_days": int|null, "created_at": str, "released_at": str|null}


def _holds_path(registry: str) -> Path:
    return Path(f".novafabric/registries/{registry}/holds.jsonl")


def _read_active_holds(registry: str) -> list[dict[str, object]]:
    path = _holds_path(registry)
    if not path.exists():
        return []
    return [
        h
        for h in (
            json.loads(line)
            for line in path.read_text().splitlines()
            if line.strip()
        )
        if h["released_at"] is None
    ]


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

    Scope: single registry.

    \b
    Examples:
      nova hold create my-registry --reason "Litigation hold for case #42"
      nova hold create my-registry --reason "Audit" --duration-days 90
    """
    hold_id = f"hold-{uuid.uuid4().hex[:8]}"
    path = _holds_path(registry)
    path.parent.mkdir(parents=True, exist_ok=True)
    record: dict[str, object] = {
        "hold_id": hold_id,
        "registry": registry,
        "reason": reason,
        "duration_days": duration_days,
        "created_at": datetime.now(tz=timezone.utc).isoformat(),
        "released_at": None,
    }
    with path.open("a") as f:
        f.write(json.dumps(record) + "\n")
    AuditLog(AUDIT_LOG_PATH).append(
        event_type=AuditEventType.HOLD_CREATE,
        actor="cli-user",
        resource_id=registry,
        details={"hold_id": hold_id, "reason": reason, "duration_days": duration_days},
    )
    console.print(f"[green]Hold created:[/green] {hold_id} on registry '{registry}'")
    if duration_days is None:
        console.print("  Duration: indefinite")
    else:
        console.print(f"  Duration: {duration_days} days")


@app.command("list")
def hold_list(
    registry: str = typer.Argument(..., help="Registry name"),
) -> None:
    """List all active legal holds for a registry.

    Scope: single registry.

    \b
    Examples:
      nova hold list my-registry
    """
    holds = _read_active_holds(registry)
    if not holds:
        console.print("No active holds.")
        return
    table = Table("Hold ID", "Reason", "Duration", "Created")
    for h in holds:
        duration = str(h["duration_days"]) + "d" if h["duration_days"] is not None else "indefinite"
        table.add_row(
            str(h["hold_id"]),
            str(h["reason"]),
            duration,
            str(h["created_at"])[:19],
        )
    console.print(table)


@app.command("release")
def hold_release(
    hold_id: str = typer.Argument(..., help="Hold ID to release"),
) -> None:
    """Release a legal hold, allowing capsule deletions to resume.

    Scope: single hold (searched across all registries).

    \b
    Examples:
      nova hold release <hold-id>
    """
    base = Path(".novafabric/registries")
    if not base.exists():
        console.print(f"[red]Hold not found: {hold_id}[/red]")
        raise typer.Exit(code=1)

    for holds_file in base.glob("*/holds.jsonl"):
        lines = holds_file.read_text().splitlines()
        updated_lines = []
        registry = holds_file.parent.name
        released = False
        for line in lines:
            if not line.strip():
                continue
            h = json.loads(line)
            if h["hold_id"] == hold_id and h["released_at"] is None:
                h["released_at"] = datetime.now(tz=timezone.utc).isoformat()
                released = True
            updated_lines.append(json.dumps(h))
        if released:
            tmp = holds_file.with_suffix(".tmp")
            tmp.write_text("\n".join(updated_lines) + "\n")
            os.replace(tmp, holds_file)
            AuditLog(AUDIT_LOG_PATH).append(
                event_type=AuditEventType.HOLD_RELEASE,
                actor="cli-user",
                resource_id=registry,
                details={"hold_id": hold_id},
            )
            console.print(f"[green]Released hold:[/green] {hold_id}")
            return

    console.print(f"[red]Hold not found or already released: {hold_id}[/red]")
    raise typer.Exit(code=1)
