"""`nova export --html` — shareable single-file offline capsule viewer (ADR-0140).

Experimental. Renders one capsule's non-sensitive summary as exactly one
self-contained HTML file (inline CSS, no JavaScript, zero external requests)
that opens offline from ``file://``. Read-only and non-blocking; complements —
never replaces — the signed Evidence Bundle (`nova export-evidence`).
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console

console = Console()


def export_cmd(
    capsule_dir: Annotated[
        Path,
        typer.Argument(help="Capsule directory to export"),
    ],
    html: Annotated[
        bool,
        typer.Option(
            "--html",
            help="Emit the single-file offline HTML capsule viewer (ADR-0140).",
        ),
    ] = False,
    output: Annotated[
        Optional[str],
        typer.Option(
            "--output",
            "-o",
            help="Output HTML file path (default: <capsule_dir>.html next to the capsule dir)",
        ),
    ] = None,
    title: Annotated[
        Optional[str],
        typer.Option("--title", help="Optional page title override"),
    ] = None,
) -> None:
    """Export a capsule as a shareable, self-contained offline HTML page (experimental).

    Produces ONE .html file — inline CSS, no JavaScript, zero external
    requests — rendering the capsule's non-sensitive summary (header, model
    calls, tool calls, eval scores, lineage references). It opens in any
    browser from file:// with no NovaFabric install and no server. The page
    is a human-readable view, NOT a cryptographic verifier: run `nova verify`
    (or use the signed Evidence Bundle) for real verification. Redaction is
    preserved verbatim; the exporter never un-redacts.

    \b
    Examples:
      nova export --html .novafabric/runs/01HX.../
      nova export --html .novafabric/runs/01HX.../ -o run.html --title "Nightly agent run"
    """
    if not html:
        console.print(
            "[red]x[/red] nova export currently supports only the --html renderer "
            "(ADR-0140); pass --html. For the signed archive use `nova export-evidence`."
        )
        raise typer.Exit(code=2)

    from novafabric.viewer.html import export_capsule_html

    out_path = Path(output) if output else capsule_dir.parent / f"{capsule_dir.name}.html"
    try:
        written, warnings = export_capsule_html(capsule_dir, out_path, title=title)
    except (FileNotFoundError, NotADirectoryError) as exc:
        console.print(f"[red]x[/red] {exc}")
        raise typer.Exit(code=1) from exc

    for warning in warnings:
        console.print(f"[yellow]![/yellow] {warning}")
    console.print(f"[green]✓[/green] capsule viewer written to {written}")
    console.print(
        "  note: human-readable summary — run `nova verify` "
        "(or verify the Evidence Bundle) for real verification"
    )
