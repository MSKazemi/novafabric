"""nova passport — portable agent-passport projection (ADR-0149 / NF-179).

``nova passport issue`` projects the identity / lineage / AIBOM / card / package / delegation
references NovaFabric already produces into one portable passport document, verifiable
offline as ``green`` / ``amber`` / ``red``. ``nova passport verify`` re-derives that verdict
from a passport document offline and confirms it matches the recorded status.

This first slice takes the component refs from an input document; the collector that reads
them from a sealed capsule (``--asset``) and the seal-path signing of the passport are
documented follow-ons. The projection is honest: an opaque ancestor is ``amber``, never
dressed up as ``green``.

Exit codes: ``issue`` — 0 rendered, 2 malformed input. ``verify`` — 0 when the recomputed
status matches the document, 3 on a status mismatch, 2 on malformed input.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

console = Console()
err_console = Console(stderr=True)

passport_app = typer.Typer(
    name="passport",
    help="Portable agent-passport projection (ADR-0149 / NF-179).",
    no_args_is_help=True,
)


def _load_object(document: Path) -> dict[str, object]:
    if not document.exists():
        err_console.print(f"[red]Document not found:[/red] {document}")
        raise typer.Exit(2)
    try:
        doc = json.loads(document.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        err_console.print(f"[red]Could not read document:[/red] {exc}")
        raise typer.Exit(2) from exc
    if not isinstance(doc, dict):
        err_console.print("[red]Document must be a JSON object.[/red]")
        raise typer.Exit(2)
    return doc


def _render(doc: object) -> None:
    from novafabric.interop.passport import PassportDocument

    assert isinstance(doc, PassportDocument)
    # Render the status in parens — rich would strip a bracketed [green] as a style tag.
    console.print(f"agent: {doc.agent_ref}")
    console.print(f"status: ({doc.status.value})")
    for component in doc.components:
        ref = component.ref or "—"
        console.print(f"  {component.name:<11} {component.state.value:<8} {ref}")


@passport_app.command("issue")
def passport_issue_cmd(
    document: Annotated[
        Path,
        typer.Argument(help="JSON: {agent_ref, present: {component: ref}, opaque: [...]}."),
    ],
    json_out: Annotated[
        bool, typer.Option("--json", help="Emit the passport document as JSON."),
    ] = False,
) -> None:
    """Project a portable agent passport (green/amber/red) from component refs.

    \b
    Examples:
      nova passport issue refs.json
      nova passport issue refs.json --json
    """
    from novafabric.interop.passport import build_passport

    obj = _load_object(document)
    agent_ref = obj.get("agent_ref")
    if not isinstance(agent_ref, str) or not agent_ref:
        err_console.print("[red]Missing required field:[/red] agent_ref")
        raise typer.Exit(2)
    present = obj.get("present", {})
    opaque = obj.get("opaque", [])
    if not isinstance(present, dict) or not isinstance(opaque, list):
        err_console.print("[red]`present` must be an object and `opaque` an array.[/red]")
        raise typer.Exit(2)

    try:
        passport = build_passport(
            agent_ref=agent_ref,
            present={str(k): str(v) for k, v in present.items()},
            opaque=[str(x) for x in opaque],
        )
    except ValueError as exc:
        err_console.print(f"[red]Invalid passport input:[/red] {exc}")
        raise typer.Exit(2) from exc

    if json_out:
        print(json.dumps(passport.model_dump(mode="json"), indent=2))
        raise typer.Exit(0)

    _render(passport)
    raise typer.Exit(0)


@passport_app.command("verify")
def passport_verify_cmd(
    document: Annotated[
        Path, typer.Argument(help="A passport document produced by `nova passport issue --json`."),
    ],
) -> None:
    """Re-derive the passport verdict offline and confirm it matches the document.

    \b
    Examples:
      nova passport verify agent-passport.json
    """
    from novafabric.interop.passport import PassportDocument, build_passport

    obj = _load_object(document)
    try:
        recorded = PassportDocument.model_validate(obj)
    except ValueError as exc:
        err_console.print(f"[red]Not a valid passport document:[/red] {exc}")
        raise typer.Exit(2) from exc

    from novafabric.interop.passport import ComponentState

    present = {
        c.name: c.ref
        for c in recorded.components
        if c.state is ComponentState.present and c.ref is not None
    }
    opaque = [c.name for c in recorded.components if c.state is ComponentState.opaque]
    recomputed = build_passport(agent_ref=recorded.agent_ref, present=present, opaque=opaque)

    _render(recomputed)
    if recomputed.status is not recorded.status:
        err_console.print(
            f"[red]Status mismatch:[/red] document=({recorded.status.value}) "
            f"recomputed=({recomputed.status.value})"
        )
        raise typer.Exit(3)
    console.print(f"[green]verified[/green] — status ({recomputed.status.value}) matches")
    raise typer.Exit(0)
