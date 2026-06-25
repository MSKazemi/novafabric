from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console

from novafabric.registry.service import DuplicateAssetError, register_asset
from novafabric.spec.validator import (
    SpecValidationError,
    print_spec_error,
    validate_spec,
)

console = Console()


def register_cmd(
    spec_file: Path = typer.Argument(..., help="Path to asset YAML spec"),
) -> None:
    """Register a new asset or version from a YAML spec file.

    The spec must pass schema validation before the asset is written.
    Run `nova validate` first to check the spec without committing it.

    Scope: registry-wide (adds or updates one asset entry).

    \b
    Examples:
      # Register from a spec file
      nova register assets/my-agent.yaml

      # Validate first, then register
      nova validate assets/my-agent.yaml && nova register assets/my-agent.yaml
    """
    try:
        spec = validate_spec(spec_file)
    except SpecValidationError as exc:
        print_spec_error(console, spec_file, exc)
        raise typer.Exit(code=1)

    try:
        result = register_asset(spec, spec_file)
    except DuplicateAssetError as exc:
        console.print(f"[red]Registration failed:[/red] {exc}")
        raise typer.Exit(code=1)

    console.print(
        f"[green]Registered[/green] {result['name']}@{result['version']} "
        f"(id: {result['id']})"
    )
