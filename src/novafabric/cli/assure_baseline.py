"""nova assure-baseline — pin and re-verify golden baselines (ADR-0147 D1, NF-160).

``pin`` designates one or more sealed capsules as the fixed reference a drift
detector measures against, binding each to its capsule Merkle root. ``verify``
recomputes those roots offline and reports whether the pinned bytes are still
the bytes on disk.

Read-only with respect to the capsule: ``pin`` emits the facet, it does not
re-seal anything. Both commands exit ``0`` on a successful *check* — including a
check that found a mismatch, which is an observation, not a gate — and ``2`` only
on bad input. ``verify`` exits ``1`` when a pinned root no longer matches, because
that one *is* a failed verification rather than a recorded observation.

The command name is hyphenated because ``nova assure`` is already a plain
command, so the spec's ``nova assure baseline …`` group form is blocked — the
same constraint ``cli/assure_coverage.py`` records for ``assure coverage``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console

from novafabric.assure._honesty import HONESTY_LINE
from novafabric.assure.baseline import (
    CRITERIA,
    BaselineError,
    attach_facet,
    baseline_run_from_capsule,
    facet_from_capsule,
    pin_baseline,
    verify_pin,
)

app = typer.Typer(
    name="assure-baseline",
    help="Pin and re-verify golden baselines (experimental, ADR-0147 NF-160).",
    no_args_is_help=True,
)

console = Console()
err_console = Console(stderr=True)


@app.command("pin")
def pin(
    capsule: Annotated[
        Path,
        typer.Option("--capsule", help="Directory of the sealed golden capsule."),
    ],
    run_id: Annotated[
        str, typer.Option("--run", help="Run id of the golden capsule.")
    ],
    baseline_id: Annotated[
        str, typer.Option("--id", help="Identifier for this baseline.")
    ],
    criterion: Annotated[
        str,
        typer.Option(
            "--criterion",
            help=f"Which axis this is a baseline for: {' | '.join(CRITERIA)}.",
        ),
    ],
    pinned_at: Annotated[
        str, typer.Option("--pinned-at", help="RFC 3339 timestamp for the pin.")
    ],
    out: Annotated[
        Path | None,
        typer.Option("--out", help="Write the pin as JSON to this path."),
    ] = None,
) -> None:
    """Designate a sealed capsule as a golden baseline."""
    if not capsule.is_dir():
        err_console.print(f"[red]Capsule directory not found:[/red] {capsule}")
        raise typer.Exit(2)
    try:
        run = baseline_run_from_capsule(run_id, capsule)
        record = pin_baseline(baseline_id, [run], criterion, pinned_at=pinned_at)
    except BaselineError as exc:
        err_console.print(f"[red]Cannot pin baseline:[/red] {exc}")
        raise typer.Exit(2) from exc

    payload = record.model_dump(exclude_none=True)
    if out is not None:
        out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        console.print(f"[green]Pinned[/green] {baseline_id} -> {out}")
    else:
        console.print_json(json.dumps(payload))
    console.print(f"[dim]{HONESTY_LINE}[/dim]")


@app.command("verify")
def verify(
    pin_file: Annotated[
        Path, typer.Option("--pin", help="Baseline pin JSON, or a capsule holding one.")
    ],
    capsule: Annotated[
        Path,
        typer.Option("--capsule", help="Directory of the capsule to re-hash."),
    ],
    run_id: Annotated[
        str, typer.Option("--run", help="Which pinned run *capsule* corresponds to.")
    ],
) -> None:
    """Recompute a pinned capsule's sealed root and report whether it still matches."""
    if not pin_file.is_file():
        err_console.print(f"[red]Pin file not found:[/red] {pin_file}")
        raise typer.Exit(2)
    if not capsule.is_dir():
        err_console.print(f"[red]Capsule directory not found:[/red] {capsule}")
        raise typer.Exit(2)

    try:
        doc: Any = json.loads(pin_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        err_console.print(f"[red]Could not read pin:[/red] {exc}")
        raise typer.Exit(2) from exc
    if not isinstance(doc, dict):
        err_console.print("[red]Pin document must be a JSON object.[/red]")
        raise typer.Exit(2)

    try:
        # Accept either a bare pin or a capsule carrying one in facets.baseline.
        record = facet_from_capsule(doc)
        if record is None:
            from novafabric.assure.baseline import BaselinePin

            record = BaselinePin.model_validate(doc)
        results = verify_pin(record, {run_id: capsule})
    except (BaselineError, ValueError) as exc:
        err_console.print(f"[red]Invalid baseline pin:[/red] {exc}")
        raise typer.Exit(2) from exc

    if not results:
        err_console.print(
            f"[yellow]Run {run_id!r} is not in this pin — nothing verified.[/yellow]"
        )
        raise typer.Exit(2)

    result = results[0]
    console.print_json(json.dumps(result.model_dump()))
    console.print(f"[dim]{HONESTY_LINE}[/dim]")
    if not result.matches:
        raise typer.Exit(1)


__all__ = ["app", "attach_facet"]
