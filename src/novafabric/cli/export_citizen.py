"""nova export-citizen-explanation — subject-facing decision explanation (ADR-0169 D1 / NF-377).

Read-only. Loads a decision's recorded factors and human-involvement level and renders a
plain-language, subject-facing explanation of *meaningful information*: ``decision_ref``, the
recorded non-secret ``factors``, ``human_involvement`` (``solely_automated`` / ``human_in_the_loop``
/ ``human_reviewed``), ``contest_channel_ref``, and ``logic_summary_ref``.

It records meaningful information; it **never claims the explanation is legally sufficient**, and it
**refuses** any factor that exposes model internals (weights/logits/activations/…) or a raw
sensitive identifier — such content never enters a public explanation.

Exit codes: 0 — rendered; 2 — the input is missing/malformed, ``human_involvement`` is invalid, or a
factor is refused.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

console = Console()
err_console = Console(stderr=True)


def export_citizen_explanation_cmd(
    document: Annotated[
        Path,
        typer.Argument(
            help="JSON: {decision_ref, factors[], human_involvement, contest_channel_ref?, "
            "logic_summary_ref?}."
        ),
    ],
    json_out: Annotated[
        bool,
        typer.Option("--json", help="Emit the explanation as JSON."),
    ] = False,
) -> None:
    """Render a plain-language, subject-facing decision explanation.

    \b
    Examples:
      nova export-citizen-explanation cit.json
      nova export-citizen-explanation cit.json --json
    """
    from novafabric.compliance.export.public._citizen import build_citizen_explanation

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

    try:
        explanation = build_citizen_explanation(
            decision_ref=doc.get("decision_ref"),
            factors=doc.get("factors", []),
            human_involvement=doc.get("human_involvement"),
            contest_channel_ref=doc.get("contest_channel_ref"),
            logic_summary_ref=doc.get("logic_summary_ref"),
        )
    except (ValueError, TypeError) as exc:
        # Includes the invalid-involvement and model-internals/identifier refusals (exit 2).
        err_console.print(f"[red]Refused:[/red] {exc}")
        raise typer.Exit(2) from exc

    if json_out:
        print(json.dumps(explanation.model_dump(mode="json"), indent=2))
        raise typer.Exit(0)

    console.print("Citizen decision explanation (meaningful information — not a legal sufficiency)")
    console.print(f"  decision_ref:        {explanation.decision_ref}")
    console.print(f"  human_involvement:   {explanation.human_involvement}")
    console.print(f"  contest_channel_ref: {explanation.contest_channel_ref}")
    console.print(f"  logic_summary_ref:   {explanation.logic_summary_ref}")
    console.print(f"  factors ({len(explanation.factors)}):")
    for f in explanation.factors:
        console.print(f"    • {f}")

    raise typer.Exit(0)
