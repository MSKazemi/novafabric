"""C-1.4 — nova rollback <asset-name>

Find the most recent previous production version of an asset, archive the
current production version, and promote the previous one back to production,
all in a single atomic DB transaction with a full audit trail.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from novafabric.registry.service import (
    AssetNotFoundError,
    RollbackError,
    rollback_asset,
)

console = Console()
log = logging.getLogger(__name__)


def rollback_cmd(
    asset_name: str = typer.Argument(..., help="Asset name to roll back"),
    to: Optional[str] = typer.Option(
        None,
        "--to",
        help="Explicit target version to restore (e.g. v1.8.0). "
        "If omitted, the most recent previous production version is used.",
    ),
    actor: str = typer.Option(
        "cli-user",
        "--actor",
        help="Identity of the operator performing the rollback (for audit trail).",
    ),
    db_path: Optional[Path] = typer.Option(
        None,
        "--db",
        help=(
            "Override the registry DB path "
            "(default: NOVAFABRIC_DB_PATH or ~/.novafabric/registry.db)"
        ),
        envvar="NOVAFABRIC_DB_PATH",
    ),
) -> None:
    """Roll back an asset to its most recent production version.

    Archives the current production version and atomically restores the
    previous one. Use --to to target a specific prior version.
    Leaves a full audit trail.

    Scope: single asset.

    \b
    Examples:
      # Roll back to the most recent prior production version
      nova rollback my-agent

      # Roll back to a specific version
      nova rollback my-agent --to v1.8.0

      # Roll back as a named actor
      nova rollback my-agent --actor carol@example.com
    """
    try:
        result = rollback_asset(
            name=asset_name,
            target_version=to,
            actor=actor,
            db_path=db_path,
        )
    except AssetNotFoundError as exc:
        console.print(f"[red]Asset not found:[/red] {exc}")
        raise typer.Exit(code=1)
    except RollbackError as exc:
        console.print(f"[red]Rollback failed:[/red] {exc}")
        raise typer.Exit(code=1)

    # Pretty confirmation table
    table = Table(title=f"Rollback — {asset_name}", show_header=True)
    table.add_column("Action", style="bold")
    table.add_column("Version", style="cyan")
    table.add_column("Status after")

    table.add_row(
        "[yellow]Archived[/yellow]",
        result["archived_version"],
        "[yellow]archived[/yellow]",
    )
    table.add_row(
        "[green]Promoted[/green]",
        result["restored_version"],
        "[green]production[/green]",
    )
    console.print(table)

    if result.get("skipped_archived"):
        console.print(
            "[yellow]Warning:[/yellow] one or more intermediate production versions "
            "were already archived and were skipped."
        )
    console.print(
        f"\n[green]✓ Rollback complete.[/green] "
        f"actor={actor!r}  rollback_id={result['rollback_id']!r}"
    )
