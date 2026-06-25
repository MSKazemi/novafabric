from __future__ import annotations

from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console

console = Console()


def export_rocrate_cmd(
    capsule_dir: Annotated[
        Path,
        typer.Argument(help="Capsule directory to package as RO-Crate v1.1"),
    ],
    output: Annotated[
        Optional[str],
        typer.Option(
            "--output",
            "-o",
            help="Output ZIP file path (default: <capsule_dir>.rocrate.zip)",
        ),
    ] = None,
) -> None:
    """Export a capsule as an RO-Crate v1.1 ZIP archive.

    RO-Crate (Research Object Crate) is a W3C community standard for
    packaging research data with structured metadata. Use for publishing
    runs to scientific repositories or data archives.

    Scope: single capsule.

    \b
    Examples:
      nova export-rocrate path/to/my-capsule/
      nova export-rocrate path/to/my-capsule/ --output my-run.crate.zip
    """
    from novafabric.compliance.export.ro_crate import export_ro_crate

    out_path = Path(output) if output else capsule_dir.parent / f"{capsule_dir.name}.rocrate.zip"

    try:
        written = export_ro_crate(capsule_dir, out_path)
    except (FileNotFoundError, NotADirectoryError) as exc:
        console.print(f"[red]x[/red] {exc}")
        raise typer.Exit(code=1) from exc

    console.print(f"[green]✓[/green] RO-Crate v1.1 written to {written}")
