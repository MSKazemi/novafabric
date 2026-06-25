from __future__ import annotations

import getpass

import typer
from rich.console import Console

from novafabric.registry.service import (
    AssetNotFoundError,
    InvalidLifecycleTransitionError,
    approve_asset,
)

console = Console()


def approve_cmd(
    asset_ref: str = typer.Argument(..., help="Asset ref as name@version"),
    approver: str = typer.Option(
        default_factory=getpass.getuser,
        help="Approver identity (defaults to OS username)",
    ),
    note: str = typer.Option("", "--note", help="Approval note"),
) -> None:
    """Record a sign-off approval for an asset awaiting promotion.

    Assets in 'pending_approval' status must be approved before they
    can move to 'staging' or 'production'.

    Scope: single asset.

    \b
    Examples:
      # Approve (OS username used as approver identity)
      nova approve my-agent@v1.1

      # Approve with explicit identity and a note
      nova approve my-agent@v1.1 --approver alice --note "Reviewed model card and evals"
    """
    if "@" not in asset_ref:
        console.print("[red]Must specify version: name@version[/red]")
        raise typer.Exit(code=1)
    name, version = asset_ref.rsplit("@", 1)
    try:
        result = approve_asset(name, version, approver=approver, note=note)
    except AssetNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)
    except InvalidLifecycleTransitionError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)
    console.print(
        f"[green]Approval recorded[/green] for {name}@{version} by {result['approver']}"
    )
