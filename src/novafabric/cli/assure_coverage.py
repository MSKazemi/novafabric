"""nova assure-coverage — structural coverage of an assurance case (ADR-0166 D4, NF-348).

Read-only. Loads an *assurance-case document* — the same JSON bundle ``nova assure-case`` reads (the
argument graph plus optional ``resolvable_digests``, ``currency`` ledger, and ``defeaters``) — and
reports the D4 structural coverage: ``total_goals``, ``goals_with_resolvable_leaf``,
``unsupported_leaves``, ``open_defeaters``, ``overdue_nodes``.

Per ADR-0166 D4 this reports coverage and **never a pass/fail grade or a numeric assurance score**:
there is no ok/grade/score field and — unlike ``nova assure-case`` — the command exits 0 whenever
it renders (open defeaters and unsupported leaves are coverage facts, not a failing verdict). It
exits 2 only when the input is missing/malformed, or a currency ledger is present without
``--as-of`` (the system clock is never used — ADR-0166 D2).

(The ADR's eventual ``nova assure coverage`` group form waits on an ``assure`` command group; this
first slice ships the read as a top-level command, matching the shipped ``nova assure-case``.)
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

console = Console()
err_console = Console(stderr=True)


def _parse_as_of(raw: str) -> datetime:
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def assure_coverage_cmd(
    document: Annotated[
        Path,
        typer.Argument(help="Path to an assurance-case document (JSON)."),
    ],
    as_of: Annotated[
        str | None,
        typer.Option(
            "--as-of",
            help="ISO-8601 instant to evaluate currency at (required if the document carries a "
            "currency ledger; never inferred from the system clock).",
        ),
    ] = None,
    json_out: Annotated[
        bool,
        typer.Option("--json", help="Emit the coverage report as JSON."),
    ] = False,
) -> None:
    """Report structural coverage of an assurance case — counts and gaps, never a grade.

    \b
    The document is a JSON object with:
      case                 (required) the argument graph: {case_id, nodes[]}
      resolvable_digests   (optional) evidence digests that currently resolve
      currency             (optional) {nodes[]} — a currency ledger (needs --as-of)
      defeaters            (optional) recorded challenges to nodes

    \b
    Examples:
      nova assure-coverage case.json
      nova assure-coverage case.json --as-of 2026-06-01T00:00:00Z --json
    """
    from novafabric.assure.case import AssuranceCase
    from novafabric.assure.coverage import compute_argument_coverage
    from novafabric.assure.currency import CurrencyLedger
    from novafabric.assure.defeater import Defeater

    if not document.exists():
        err_console.print(f"[red]Assurance-case document not found:[/red] {document}")
        raise typer.Exit(2)
    try:
        doc = json.loads(document.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        err_console.print(f"[red]Could not read document:[/red] {exc}")
        raise typer.Exit(2) from exc
    if not isinstance(doc, dict) or "case" not in doc:
        err_console.print("[red]Document must be a JSON object with a 'case' field.[/red]")
        raise typer.Exit(2)

    ledger: CurrencyLedger | None = None
    at: datetime | None = None
    try:
        case = AssuranceCase.model_validate(doc["case"])
        defeaters = [Defeater.model_validate(d) for d in doc.get("defeaters", [])]
        if "currency" in doc:
            if as_of is None:
                err_console.print(
                    "[red]This document carries a currency ledger; pass --as-of "
                    "<ISO-8601> to evaluate it (the system clock is never used).[/red]"
                )
                raise typer.Exit(2)
            ledger = CurrencyLedger.model_validate(doc["currency"])
            at = _parse_as_of(as_of)
    except typer.Exit:
        raise
    except Exception as exc:  # pydantic ValidationError / bad --as-of → malformed input (exit 2)
        err_console.print(f"[red]Invalid coverage input:[/red] {exc}")
        raise typer.Exit(2) from exc

    coverage = compute_argument_coverage(
        case,
        resolvable_digests=frozenset(doc.get("resolvable_digests", [])),
        defeaters=defeaters,
        ledger=ledger,
        as_of=at,
    )

    if json_out:
        print(json.dumps(coverage.model_dump(mode="json"), indent=2))
        raise typer.Exit(0)

    console.print(
        f"[bold]Argument coverage[/bold] for {case.case_id} "
        "(structural — never a grade or score):"
    )
    console.print(
        f"  goals with a resolvable leaf: {coverage.goals_with_resolvable_leaf}"
        f" / {coverage.total_goals}"
    )
    console.print(f"  unsupported leaves: {len(coverage.unsupported_leaves)}")
    for leaf in coverage.unsupported_leaves:
        console.print(f"    [yellow]unsupported:[/yellow] {leaf}")
    console.print(f"  open defeaters: {coverage.open_defeaters}")
    console.print(f"  overdue nodes: {len(coverage.overdue_nodes)}")
    for nid in coverage.overdue_nodes:
        console.print(f"    [red]overdue:[/red] {nid}")

    raise typer.Exit(0)
