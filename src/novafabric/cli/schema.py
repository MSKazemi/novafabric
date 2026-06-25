"""CLI commands for schema inspection (cap-001, ADR-0066)."""

from __future__ import annotations

import typer

from novafabric.schemas.event_schema import CapsuleEventType

app = typer.Typer(
    help="List and inspect capsule event schema types.",
    no_args_is_help=True,
)


@app.command("list")
def schema_list_cmd() -> None:
    """List all capsule event types in the current schema.

    Scope: global.

    \b
    Examples:
      nova schema list
    """
    for et in CapsuleEventType:
        typer.echo(et.value)
