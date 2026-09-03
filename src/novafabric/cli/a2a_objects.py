"""nova a2a-objects — A2A Task/Message/Artifact mapping (ADR-0149 D1, NF-172).

``map`` maps A2A objects onto capsule entities and records the field-by-field
manifest plus a ``roundtrip_digest``. ``roundtrip`` re-exports and checks that
digest, naming the diverging object when it fails.

The digest is taken over the **re-exported** objects, not over the stored facet,
so the check genuinely exercises map → store → export. A `parts` array is bound by
digest and never stored, so a re-export carries ``parts_digest`` in its place — a
bounded reconstruction rather than a fabricated one.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console

from novafabric.a2a.objects import (
    A2AObjectsError,
    A2AObjectsFacet,
    facet_from_capsule,
    map_objects,
    roundtrip,
)

app = typer.Typer(
    name="a2a-objects",
    help="A2A Task/Message/Artifact object mapping (experimental, ADR-0149 NF-172).",
    no_args_is_help=True,
)

console = Console()
err_console = Console(stderr=True)


def _load(path: Path, what: str) -> dict[str, Any]:
    if not path.is_file():
        err_console.print(f"[red]{what} not found:[/red] {path}")
        raise typer.Exit(2)
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        err_console.print(f"[red]Could not read {what.lower()}:[/red] {exc}")
        raise typer.Exit(2) from exc
    if not isinstance(doc, dict):
        err_console.print(f"[red]{what} must be a JSON object.[/red]")
        raise typer.Exit(2)
    return doc


@app.command("map")
def map_cmd(
    objects: Annotated[
        Path,
        typer.Option(
            "--objects",
            help='JSON: {"tasks": [...], "messages": [...], "artifacts": [...]}',
        ),
    ],
    out: Annotated[
        Path | None, typer.Option("--out", help="Write the facet JSON here.")
    ] = None,
) -> None:
    """Map A2A objects onto capsule entities."""
    doc = _load(objects, "Objects document")
    try:
        facet = map_objects(
            tasks=doc.get("tasks", []),
            messages=doc.get("messages", []),
            artifacts=doc.get("artifacts", []),
        )
    except A2AObjectsError as exc:
        err_console.print(f"[red]Cannot map objects:[/red] {exc}")
        raise typer.Exit(2) from exc

    if facet is None:
        err_console.print("[yellow]No A2A objects supplied — no facet written.[/yellow]")
        raise typer.Exit(0)

    payload = facet.model_dump(exclude_none=True)
    if out is not None:
        out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        console.print(f"[green]Mapped[/green] -> {out}")
    else:
        console.print_json(json.dumps(payload))

    unmapped = sum(len(o.unmapped) for o in facet.all_objects())
    if unmapped:
        console.print(
            f"[yellow]{unmapped} field(s) are not carried by this mapping[/yellow] "
            "— listed per object in `unmapped[]`, never dropped silently."
        )


@app.command("roundtrip")
def roundtrip_cmd(
    facet_file: Annotated[
        Path, typer.Option("--facet", help="A facet JSON, or a capsule carrying one.")
    ],
) -> None:
    """Re-export the mapped objects and assert the round-trip digest."""
    doc = _load(facet_file, "Facet")
    try:
        facet = facet_from_capsule(doc)
        if facet is None:
            facet = A2AObjectsFacet.model_validate(doc)
        result = roundtrip(facet)
    except (A2AObjectsError, ValueError) as exc:
        err_console.print(f"[red]Invalid a2a_objects facet:[/red] {exc}")
        raise typer.Exit(2) from exc

    console.print_json(json.dumps(result.model_dump()))
    if not result.matches:
        for d in result.diverging:
            err_console.print(
                f"[red]diverged:[/red] {d['kind']} {d['identity']!r} "
                f"(fields: {', '.join(d['fields'])})"
            )
        raise typer.Exit(1)
