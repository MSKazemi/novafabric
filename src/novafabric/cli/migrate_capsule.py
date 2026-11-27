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
            help="Source capsule directory (schema_version 0.1.0).",
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
    """Convert a schema_version 0.1.0 capsule to the 1.0.0 format.

    Copies SOURCE to --output, bumps every schema_version 0.1.0 field to
    1.0.0 and appends a migration_source lineage edge to the copy. The
    original source directory is not modified. Only 0.1.0 (what every writer
    stamps today) is accepted; any other source version exits 1.

    A source holding a symbolic link is refused (exit 1, no output): a
    migration never follows a link.

    Scope: single capsule directory.

    \b
    Examples:
      nova migrate path/to/old-capsule/ -o path/to/new-capsule/
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
