# src/novafabric/cli/diagnose.py
from __future__ import annotations

import json
from enum import Enum
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.table import Table

from novafabric._paths import default_capsule_dir
from novafabric.diagnose import CapsuleNotFoundError, attribute_failure
from novafabric.diagnose.verify import HypothesisVerification

console = Console()

_VERDICT_STYLES = {
    "CONFIRMED": "green",
    "REFUTED": "red",
    "INCONCLUSIVE": "yellow",
}


class DiagnoseOutputFormat(str, Enum):
    text = "text"
    json = "json"


def diagnose_cmd(
    run_id: Annotated[
        str, typer.Argument(help="Run id of the failed capsule to diagnose.")
    ],
    capsule_dir: Annotated[
        Optional[Path],
        typer.Option(
            "--capsule-dir",
            help="Capsule storage directory (defaults to $NOVAFABRIC_CAPSULE_DIR).",
        ),
    ] = None,
    output: Annotated[
        DiagnoseOutputFormat,
        typer.Option("--output", "-o", help="Output format."),
    ] = DiagnoseOutputFormat.text,
    intervene: Annotated[
        bool,
        typer.Option(
            "--intervene",
            help=(
                "Experimental (ADR-0101): verify the top hypothesis with a "
                "counterfactual intervention replay (ADR-0086) and record an "
                "evidence-based CONFIRMED / REFUTED / INCONCLUSIVE verdict."
            ),
        ),
    ] = False,
    replay_dir: Annotated[
        Optional[Path],
        typer.Option(
            "--replay-dir",
            help=(
                "Base directory for the intervention replay output "
                "(default: .novafabric/replays). Only with --intervene."
            ),
        ),
    ] = None,
) -> None:
    """Attribute a failed run to its most likely responsible step (ADR-0084).

    Walks the captured trace plus the lineage / causal graph and produces a
    ranked attribution: which agent/step is most likely responsible, plus an
    AgentErrorTaxonomy label (MEMORY / REFLECTION / PLANNING / ACTION / SYSTEM /
    UNKNOWN). Scores are relative ranking weights, not probabilities.

    With --intervene (experimental, ADR-0101) the top hypothesis is additionally
    tested: an InterventionSpec is auto-synthesized, the capsule is replayed
    counterfactually under mocked semantics (ADR-0086, zero-token), and the
    verdict records whether the intervention flipped the outcome — evidence-based,
    never guessed.

    Scope: single failed run capsule.

    \b
    Examples:
      # Diagnose a failed run in the default capsule directory
      nova diagnose run-2026-06-11-abc123

      # Diagnose a capsule stored elsewhere, as JSON
      nova diagnose run-xyz --capsule-dir ./capsules --output json

      # Verify the top hypothesis with a counterfactual replay (experimental)
      nova diagnose run-xyz --intervene
    """
    if replay_dir is not None and not intervene:
        console.print("[red]--replay-dir only applies with --intervene[/red]")
        raise typer.Exit(code=1)

    base = (capsule_dir or default_capsule_dir()).resolve()
    cap = base / run_id

    # Best-effort lineage store for the causal-depth bias; absent/empty is fine.
    lineage_store = None
    try:
        from novafabric.lineage._store import LineageStore

        lineage_store = LineageStore()
    except Exception:
        lineage_store = None

    try:
        result = attribute_failure(cap, lineage_store=lineage_store)
    except CapsuleNotFoundError:
        console.print(
            f"[red]No capsule found for run id '{run_id}' under {base}[/red]"
        )
        raise typer.Exit(code=1)

    verification: HypothesisVerification | None = None
    if intervene:
        from novafabric.diagnose.verify import verify_hypothesis

        verification = verify_hypothesis(cap, result, replays_base_dir=replay_dir)

    if output is DiagnoseOutputFormat.json:
        payload = result.as_dict()
        if verification is not None:
            payload["verification"] = verification.as_dict()
        console.print_json(json.dumps(payload))
        return

    head = f"[bold]Failure attribution for[/bold] {result.run_id} " \
        f"([yellow]{result.status}[/yellow])"
    console.print(head)
    if result.responsible is None:
        console.print(
            f"[yellow]Taxonomy:[/yellow] {result.taxonomy.value} — {result.note}"
        )
        if verification is not None:
            _print_verification(verification)
        return

    console.print(
        f"[bold]Most likely responsible:[/bold] "
        f"[red]{result.responsible.step_id}[/red] "
        f"({result.responsible.name}) — "
        f"[magenta]{result.taxonomy.value}[/magenta]"
    )

    table = Table(title="Ranked step attribution")
    table.add_column("rank", justify="right")
    table.add_column("step_id")
    table.add_column("name")
    table.add_column("kind")
    table.add_column("score", justify="right")
    table.add_column("taxonomy")
    for i, step in enumerate(result.ranked):
        table.add_row(
            str(i),
            step.step_id,
            step.name,
            step.kind,
            f"{step.score:.2f}",
            step.taxonomy.value,
        )
    console.print(table)
    console.print(f"[dim]{result.note}[/dim]")
    if verification is not None:
        _print_verification(verification)


def _print_verification(v: HypothesisVerification) -> None:
    """Human-readable NF-022 verdict block (ADR-0101, experimental)."""
    style = _VERDICT_STYLES.get(v.verdict.value, "yellow")
    console.print()
    console.print(
        "[bold]Hypothesis verification "
        "(counterfactual, ADR-0101 — experimental)[/bold]"
    )
    console.print(f"  Verdict: [{style}]{v.verdict.value}[/{style}]")
    if v.hypothesis is not None:
        console.print(
            f"  Hypothesis: {v.hypothesis['step_id']} "
            f"({v.hypothesis['name']}) — {v.hypothesis['taxonomy']}"
        )
    if v.intervention is not None:
        selector = v.intervention.get("selector", {})
        where = selector.get("event_index", selector.get("span_id"))
        console.print(
            f"  Intervention: {v.intervention['substitution']} "
            f"on {v.intervention['stream']}[{where}]"
        )
    else:
        console.print("  Intervention: none applied")
    original = v.original_outcome.get("status")
    if v.counterfactual_outcome is not None:
        cf = v.counterfactual_outcome
        exit_part = (
            f" (exit code {cf['exit_code']})"
            if cf.get("exit_code") is not None
            else ""
        )
        console.print(
            f"  Outcome: original={original} -> "
            f"counterfactual={cf['status']}{exit_part}"
        )
    else:
        console.print(f"  Outcome: original={original} -> counterfactual=not run")
    if v.intervened_capsule is not None:
        console.print(f"  Intervened capsule: {v.intervened_capsule}")
    console.print(f"  {v.reason}")
    console.print(f"  [dim]{v.note}[/dim]")
