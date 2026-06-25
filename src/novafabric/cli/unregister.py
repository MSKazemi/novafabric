from __future__ import annotations

import getpass
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console

from novafabric.registry.service import (
    AssetNotFoundError,
    UnregisterBlockedError,
    unregister_asset,
)

console = Console()


def unregister_cmd(
    asset_ref: str = typer.Argument(..., help="Asset ref in name@version format."),
    actor: str = typer.Option(
        default_factory=getpass.getuser,
        help="Identity recorded in the audit trail (defaults to OS username).",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Override status guard; allows deletion of staging/production assets.",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip interactive confirmation (for CI/scripts).",
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
    """Remove an asset version from the registry.

    The asset record is deleted but an audit trail entry is preserved.
    Production and staging assets require --force to prevent accidental deletion.

    Scope: single asset.

    \b
    Examples:
      # Remove a development asset (interactive confirmation)
      nova unregister my-agent@v0.1.0

      # Skip confirmation (CI mode)
      nova unregister my-agent@v0.1.0 --yes

      # Force-remove a production asset
      nova unregister my-agent@v1.2.0 --force --yes
    """
    if "@" not in asset_ref:
        console.print("[red]Error:[/red] Asset ref must include version: name@version")
        raise typer.Exit(code=1)

    name, version = asset_ref.rsplit("@", 1)

    if not yes:
        confirmed = typer.confirm(
            f"Permanently delete {name}@{version} from the registry?"
        )
        if not confirmed:
            console.print("[yellow]Aborted.[/yellow]")
            raise typer.Exit(code=0)

    try:
        unregister_asset(
            name=name,
            version=version,
            actor=actor,
            force=force,
            db_path=db_path,
        )
    except AssetNotFoundError as exc:
        console.print(f"[red]Not found:[/red] {exc}")
        raise typer.Exit(code=1)
    except UnregisterBlockedError as exc:
        console.print(f"[red]Blocked:[/red] {exc}")
        raise typer.Exit(code=1)

    console.print(f"[green]Unregistered[/green] {name}@{version} (actor={actor!r})")
