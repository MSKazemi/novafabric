from __future__ import annotations

from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console

console = Console()


def export_c2pa_cmd(
    capsule_dir: Annotated[
        Path,
        typer.Argument(help="Capsule directory to export as C2PA manifest"),
    ],
    output: Annotated[
        Optional[str],
        typer.Option(
            "--output",
            "-o",
            help="Output JSON file path (default: <capsule_dir>/c2pa-manifest.json)",
        ),
    ] = None,
    training_mining: Annotated[
        bool,
        typer.Option(
            "--training-mining/--no-training-mining",
            help="Include c2pa.training-mining: notAllowed assertion (opt-in)",
        ),
    ] = False,
) -> None:
    """Export a C2PA v2.3 provenance manifest from a capsule.

    C2PA manifests document the AI origin of generated content. Required
    for EU AI Act Art.50 transparency obligations for generative AI
    (deadline 2026-08-02).

    Scope: single capsule.

    \b
    Examples:
      nova export-c2pa path/to/my-capsule/
      nova export-c2pa path/to/my-capsule/ --output manifest.c2pa.json
    """
    from novafabric.evidence.c2pa_exporter import export_c2pa

    out_path = Path(output) if output else capsule_dir / "c2pa-manifest.json"

    try:
        written = export_c2pa(
            capsule_dir, out_path, include_training_mining=training_mining
        )
    except (FileNotFoundError, ValueError) as exc:
        console.print(f"[red]x[/red] {exc}")
        raise typer.Exit(code=1) from exc

    console.print(f"[green]✓[/green] C2PA manifest written to {written}")
    console.print(
        "[dim]Note: manifest is structurally valid for TSP signing. "
        "C2PA hard-binding requires enrollment with a C2PA-certified Trust Service Provider.[/dim]"
    )
