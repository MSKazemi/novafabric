"""nova export-adverse-action — ECOA / Reg B specific-reasons evidence pack (ADR-0159 D6 / NF-278).

Read-only. Reads one sealed Run Capsule (by run id or path) and renders the evidence a creditor's
compliance team uses to author an adverse-action notice: the recorded model call(s) (model ref and
input/output digests — never the prompt or response text), the capsule's ``inputs/`` bound to its
sealed digests, and the recorded ``facets.feature_attribution`` principal reasons **in the order
and rank recorded** (never re-ranked or computed). A capsule without an attribution facet yields
``principal_reasons: missing`` with the reason.

It is evidence of what was recorded — not a notice, not a credit decision, not a legal verdict.

Exit codes: 0 — rendered (including ``missing`` rows); 2 — the capsule cannot be found, or its
sealed evidence is unreadable / malformed / does not match its recorded digests, or ``--out``
points inside the capsule.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.markup import escape

console = Console()
err_console = Console(stderr=True)

_MARK = {"complete": "[green]✓[/green]", "partial": "[yellow]◐[/yellow]", "missing": "[red]✗[/red]"}


def export_adverse_action_cmd(
    run_id: Annotated[
        str,
        typer.Option("--run-id", help="Run id (resolved in the capsule dir) or capsule path."),
    ],
    capsule_dir: Annotated[
        Path | None,
        typer.Option(
            "--capsule-dir",
            help="Capsule store to resolve --run-id in (default: $NOVAFABRIC_CAPSULE_DIR or "
            "$NOVAFABRIC_HOME/capsules).",
        ),
    ] = None,
    json_out: Annotated[
        bool,
        typer.Option("--json", help="Emit the evidence pack as JSON on stdout."),
    ] = False,
    out: Annotated[
        Path | None,
        typer.Option("--out", help="Also write the evidence pack JSON to this file."),
    ] = None,
) -> None:
    """Render the ECOA / Reg B specific-principal-reasons evidence pack for a credit run.

    Evidence of what the sealed capsule recorded — NOT an adverse-action notice,
    not a credit decision, no legal verdict. Reasons are rendered as recorded,
    never re-ranked or computed; a capsule without an attribution facet reports
    principal_reasons as missing.

    \b
    Examples:
      nova export-adverse-action --run-id 01KZ...
      nova export-adverse-action --run-id 01KZ... --json
      nova export-adverse-action --run-id 01KZ... --out aa-pack.json
    """
    from novafabric.cli._capsule_ref import CapsuleRefError, resolve_capsule_ref
    from novafabric.compliance.export.finance.adverse_action import build_adverse_action_pack
    from novafabric.compliance.export.finance.adverse_action_collect import (
        CorruptCapsuleError,
        collect_capsule_facts,
    )

    try:
        cap = resolve_capsule_ref(run_id, capsule_dir=capsule_dir)
    except CapsuleRefError as exc:
        err_console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(2) from exc
    if out is not None and out.resolve().is_relative_to(cap.resolve()):
        err_console.print("[red]--out must not point inside the capsule (read-only export).[/red]")
        raise typer.Exit(2)
    try:
        facts = collect_capsule_facts(cap)
    except CorruptCapsuleError as exc:
        err_console.print(f"[red]Corrupt or unreadable capsule evidence:[/red] {escape(str(exc))}")
        raise typer.Exit(2) from exc

    pack = build_adverse_action_pack(facts)
    payload = json.dumps(pack.model_dump(mode="json"), indent=2, sort_keys=True)
    if out is not None:
        try:
            out.write_text(payload + "\n", encoding="utf-8")
        except OSError as exc:
            err_console.print(f"[red]Cannot write --out:[/red] {escape(str(exc))}")
            raise typer.Exit(2) from exc

    if json_out:
        print(payload)
        raise typer.Exit(0)

    s = pack.summary
    # regime in parens, not "[...]", which rich would parse as markup.
    console.print(
        f"Credit-decision specific-reasons evidence ({escape(pack.regime)})  "
        f"run: {escape(pack.run_id)}   complete={s['complete']} partial={s['partial']} "
        f"missing={s['missing']}"
    )
    for row in pack.rows:
        why = f"  ({escape('; '.join(row.reasons))})" if row.reasons else ""
        console.print(f"  {_MARK[row.status]} {row.key:<20} {row.status}{why}")
    for call in pack.model_calls:
        console.print(
            f"    model call L{call.line} {escape(call.model_call_id or '-')}  "
            f"model={escape(call.response_model or call.request_model or '-')}  "
            f"in={escape(call.input_digest or '-')}  out={escape(call.output_digest or '-')}"
        )
    for reason in pack.principal_reasons:
        label = reason.description or reason.reason_code or reason.feature or "(suppressed)"
        rank = "-" if reason.rank is None else str(reason.rank)
        console.print(
            f"    #{reason.position} rank={escape(rank)}  {escape(label)}"
            f"  feature={escape(reason.feature or '-')}"
        )
    if out is not None:
        console.print(f"Wrote {escape(str(out))}")
    console.print(f"[bold]{escape(pack.cfpb_honesty)}[/bold]")
    console.print(f"[dim]{escape(pack.banner)}[/dim]")
    raise typer.Exit(0)
