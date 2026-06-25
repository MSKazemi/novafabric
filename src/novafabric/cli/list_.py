from __future__ import annotations

from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from novafabric.registry.service import list_assets, list_stale_assets

console = Console()


def list_cmd(
    type_: Optional[str] = typer.Option(
        None, "--type", "-t", help="Filter by asset type"
    ),
    status: Optional[str] = typer.Option(
        None, "--status", "-s", help="Filter by status"
    ),
    stale: bool = typer.Option(
        False,
        "--stale",
        help=(
            "Only show assets that have had no promotion, consumption, or eval "
            "activity in the last --stale-days days (default: 30)."
        ),
    ),
    stale_days: int = typer.Option(
        30,
        "--stale-days",
        help="Number of days of inactivity that classifies an asset as stale (default: 30).",
    ),
) -> None:
    """List all registered assets and their current lifecycle status.

    Scope: registry-wide.

    \b
    Examples:
      # List all assets
      nova list

      # Filter by asset type
      nova list --type llm-agent

      # Show only production assets
      nova list --status production

      # Show assets inactive for 60+ days
      nova list --stale --stale-days 60
    """
    if stale:
        assets = list_stale_assets(
            stale_days=stale_days,
            asset_type=type_,
            status=status,
        )
        if not assets:
            console.print(
                f"[green]No stale assets found[/green] "
                f"(threshold: {stale_days} days)."
            )
            return

        table = Table(title=f"Stale Assets (>{stale_days} days inactive)")
        table.add_column("Name", style="cyan")
        table.add_column("Version")
        table.add_column("Type")
        table.add_column("Status", style="yellow")
        table.add_column("Last Activity At")
        table.add_column("Days Stale", style="red")

        for a in assets:
            last_act = a.get("last_activity_at", "")
            if last_act and len(last_act) > 19:
                last_act = last_act[:19]
            table.add_row(
                a["name"],
                a["version"],
                a["asset_type"],
                a["status"],
                last_act,
                str(a.get("days_stale", "")),
            )
        console.print(table)
        return

    assets = list_assets(asset_type=type_, status=status)
    if not assets:
        console.print("[yellow]No assets found.[/yellow]")
        return

    table = Table(title="Assets")
    table.add_column("Name", style="cyan")
    table.add_column("Version")
    table.add_column("Type")
    table.add_column("Status", style="green")
    table.add_column("Created At")

    for a in assets:
        table.add_row(
            a["name"], a["version"], a["asset_type"], a["status"],
            a["created_at"][:19] if a["created_at"] else "",
        )
    console.print(table)
