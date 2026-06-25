from __future__ import annotations

import json

import typer
from rich.console import Console
from rich.panel import Panel

from novafabric.registry.service import AssetNotFoundError, get_asset

console = Console()


def inspect_cmd(
    asset_ref: str = typer.Argument(..., help="Asset reference: name@version or name"),
) -> None:
    """Show complete metadata for a registered asset version.

    Scope: single asset.

    \b
    Examples:
      # Inspect the latest version
      nova inspect my-agent

      # Inspect a specific version
      nova inspect my-agent@v1.2
    """
    if "@" in asset_ref:
        name, version = asset_ref.rsplit("@", 1)
    else:
        name, version = asset_ref, None

    try:
        asset = get_asset(name, version)
    except AssetNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)

    spec_data = json.loads(asset.get("spec_json", "{}"))
    lines = []
    for k, v in asset.items():
        if k == "spec_json":
            continue
        lines.append(f"[bold]{k}:[/bold] {v}")
    lines.append("[bold]spec:[/bold]")
    lines.append(json.dumps(spec_data.get("spec", {}), indent=2))

    console.print(Panel("\n".join(lines), title=f"{asset['name']}@{asset['version']}"))
