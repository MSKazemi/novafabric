"""CLI command: ``nova migrate``.

Converts a v0.1.x capsule directory to v1.0.0 format (ADR-0034 §6).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from novafabric.capsule_migration._converter import CapsuleMigrationError, CapsuleMigrator

console = Console()


def migrate_capsule_cmd(
    source: Annotated[
        Path,
        typer.Argument(
            help="Path to the source capsule directory (v0.1.x).",
            show_default=False,
        ),
    ],
    output: Annotated[
        Path,
        typer.Option(
            "--output",
            "-o",
            help="Destination path for the migrated capsule. Must not already exist.",
            show_default=False,
        ),
    ],
    json_errors: Annotated[
        bool,
        typer.Option(
            "--json-errors",
            help="Emit structured JSON errors to stderr instead of human-readable messages.",
        ),
    ] = False,
) -> None:
    """Upgrade a capsule directory to the current schema format.

    Copies SOURCE to OUTPUT and bumps all schema_version fields.
    The original source directory is not modified.

    Scope: single capsule directory.

    \b
    Examples:
      nova migrate path/to/old-capsule/ path/to/new-capsule/
    """
    migrator = CapsuleMigrator()
    try:
        result = migrator.migrate(source=source, output=output)
    except CapsuleMigrationError as exc:
        if json_errors:
            json.dump(exc.as_dict(), sys.stderr)
            sys.stderr.write("\n")
        else:
            console.print(f"[red]Error:[/red] {exc}")
            if exc.field:
                console.print(f"  [dim]Field:[/dim]        {exc.field}")
            if exc.rule:
                console.print(f"  [dim]Rule:[/dim]         {exc.rule}")
            if exc.spec_section:
                console.print(f"  [dim]Spec section:[/dim] {exc.spec_section}")
        raise typer.Exit(code=1) from exc

    console.print(f"[bold green]Migrated[/bold green] {source} → {output}")
    console.print(
        f"  [dim]Files updated:[/dim]  "
        f"{', '.join(result.files_updated) if result.files_updated else 'none'}"
    )
    console.print(f"  [dim]Lineage edge:[/dim]   {result.lineage_edge_id}")
