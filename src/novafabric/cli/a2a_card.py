"""nova a2a-card — portable A2A Agent Card evidence (ADR-0149 D1, NF-171).

``capture`` records a card as the ``a2a_card`` facet and writes the standalone
portable export. ``verify`` recomputes the fingerprint from a stored facet and
reports whether the card has been altered since capture.

``signature_ok`` is deliberately **not** asserted: A2A 1.0 cards are JWS-signed and
no JWS verifier is wired here, so the facet records ``signature_status:
"unverified: no JWS verifier configured"`` rather than reporting a verdict nobody
reached. ``verify`` exits ``1`` only on a *fingerprint* mismatch — the thing it can
actually prove.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console

from novafabric.a2a.card import (
    A2ACardError,
    build_facet,
    facet_from_capsule,
    verify_facet,
    write_portable_export,
)

app = typer.Typer(
    name="a2a-card",
    help="Portable A2A Agent Card evidence (experimental, ADR-0149 NF-171).",
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


@app.command("capture")
def capture(
    card: Annotated[
        Path, typer.Option("--card", help="A2A Agent Card JSON document.")
    ],
    well_known_url: Annotated[
        str | None,
        typer.Option("--well-known-url", help="Where the card was served from."),
    ] = None,
    a2a_version: Annotated[
        str, typer.Option("--a2a-version", help="A2A protocol version.")
    ] = "1.0",
    outputs: Annotated[
        Path | None,
        typer.Option("--outputs", help="Write the portable export into this dir."),
    ] = None,
    out: Annotated[
        Path | None, typer.Option("--out", help="Write the facet JSON here.")
    ] = None,
) -> None:
    """Record an A2A Agent Card as portable capsule evidence."""
    doc = _load(card, "Card")
    try:
        facet = build_facet(doc, well_known_url=well_known_url, a2a_version=a2a_version)
    except A2ACardError as exc:
        err_console.print(f"[red]Cannot build the card facet:[/red] {exc}")
        raise typer.Exit(2) from exc

    if outputs is not None:
        written = write_portable_export(facet, outputs)
        facet = facet.model_copy(
            update={"portable_export": f"{outputs.name}/{written.name}"}
        )

    payload = facet.model_dump(exclude_none=True)
    if out is not None:
        out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        console.print(f"[green]Captured[/green] {facet.card_fingerprint} -> {out}")
    else:
        console.print_json(json.dumps(payload))

    if facet.signature_ok is None:
        console.print(f"[yellow]signature:[/yellow] {facet.signature_status}")
    if facet.missing_fields:
        console.print(
            f"[yellow]card is missing:[/yellow] {', '.join(facet.missing_fields)}"
        )


@app.command("verify")
def verify(
    facet_file: Annotated[
        Path,
        typer.Option("--facet", help="A facet JSON, or a capsule carrying one."),
    ],
) -> None:
    """Recompute the card fingerprint and report whether the card changed."""
    doc = _load(facet_file, "Facet")
    try:
        facet = facet_from_capsule(doc)
        if facet is None:
            from novafabric.a2a.card import A2ACardFacet

            facet = A2ACardFacet.model_validate(doc)
        result = verify_facet(facet)
    except (A2ACardError, ValueError) as exc:
        err_console.print(f"[red]Invalid a2a_card facet:[/red] {exc}")
        raise typer.Exit(2) from exc

    console.print_json(json.dumps(result.model_dump()))
    if result.signature_ok is None:
        console.print(f"[yellow]signature:[/yellow] {result.signature_status}")
    if not result.fingerprint_matches:
        raise typer.Exit(1)
