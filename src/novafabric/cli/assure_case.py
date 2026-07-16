"""nova assure-case — inspect an assurance-case document (ADR-0166 D6, first slice).

Read-only. Loads an *assurance-case document* — a JSON bundle of the argument graph
(:class:`AssuranceCase`) plus optional currency ledger, conformance map, and defeaters —
and reports structural validity, currency/drift, a conformance receipt, and open defeaters.

The command never prints evidence bodies; it prints only references and digests, matching
the privacy invariant of the underlying models (statements and refs, never findings/PII).

Exit codes:
  0 — the case is structurally valid AND no defeater is open;
  1 — the case is structurally invalid, OR at least one defeater is open (argument defeated);
  2 — the document is missing/malformed, or a currency ledger is present without ``--as-of``.

Currency evaluation requires an explicit ``--as-of`` timestamp: the underlying model never
reads the system clock (ADR-0166 D2), so this command refuses to guess one.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console

console = Console()
err_console = Console(stderr=True)


def _parse_as_of(raw: str) -> datetime:
    """Parse an ISO-8601 ``--as-of`` value, defaulting a naive value to UTC."""
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def assure_case_cmd(
    document: Annotated[
        Path,
        typer.Argument(help="Path to an assurance-case document (JSON)."),
    ],
    as_of: Annotated[
        str | None,
        typer.Option(
            "--as-of",
            help="ISO-8601 instant to evaluate currency at (required if the document "
            "carries a currency ledger; never inferred from the system clock).",
        ),
    ] = None,
    json_out: Annotated[
        bool,
        typer.Option("--json", help="Emit a machine-readable JSON report."),
    ] = False,
) -> None:
    """Inspect an assurance-case document: validity, currency, conformance, defeaters.

    \b
    The document is a JSON object with:
      case                 (required) the argument graph: {case_id, nodes[]}
      resolvable_digests   (optional) evidence digests that currently resolve
      currency             (optional) {nodes[]} — a currency ledger (needs --as-of)
      conformance          (optional) {entries[]} — standard/clause mappings
      defeaters            (optional) recorded challenges to nodes

    \b
    Examples:
      nova assure-case case.json
      nova assure-case case.json --as-of 2026-06-01T00:00:00Z
      nova assure-case case.json --json
    """
    # Local imports keep CLI startup cheap and avoid import cycles.
    from novafabric.assure.case import AssuranceCase, validate_case
    from novafabric.assure.conformance import ConformanceMap, conformance_receipt
    from novafabric.assure.currency import CurrencyLedger, drift_records
    from novafabric.assure.defeater import Defeater, defeated_nodes, open_defeaters

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

    try:
        case = AssuranceCase.model_validate(doc["case"])
    except Exception as exc:  # pydantic ValidationError → malformed input (exit 2)
        err_console.print(f"[red]Invalid 'case':[/red] {exc}")
        raise typer.Exit(2) from exc

    resolvable = frozenset(doc.get("resolvable_digests", []))
    validation = validate_case(case, resolvable)

    # --- currency (optional; requires --as-of) ---
    currency_drift: list[dict[str, Any]] = []
    if "currency" in doc:
        if as_of is None:
            err_console.print(
                "[red]This document carries a currency ledger; pass --as-of "
                "<ISO-8601> to evaluate it (the system clock is never used).[/red]"
            )
            raise typer.Exit(2)
        try:
            ledger = CurrencyLedger.model_validate(doc["currency"])
            at = _parse_as_of(as_of)
        except Exception as exc:
            err_console.print(f"[red]Invalid currency ledger or --as-of:[/red] {exc}")
            raise typer.Exit(2) from exc
        statuses = ledger.statuses(as_of=at)
        currency_drift = [r.model_dump(mode="json") for r in drift_records(ledger, as_of=at)]
        currency_summary = {nid: s.value for nid, s in statuses.items()}
    else:
        currency_summary = {}

    # --- defeaters + conformance (optional; malformed → exit 2 like the other inputs) ---
    try:
        defeaters = [Defeater.model_validate(d) for d in doc.get("defeaters", [])]
        cmap = (
            ConformanceMap.model_validate(doc["conformance"])
            if "conformance" in doc
            else None
        )
    except Exception as exc:
        err_console.print(f"[red]Invalid defeaters or conformance map:[/red] {exc}")
        raise typer.Exit(2) from exc

    open_list = open_defeaters(defeaters)
    defeated = sorted(defeated_nodes(defeaters))

    receipt: dict[str, Any] | None = None
    if cmap is not None:
        receipt = conformance_receipt(
            case, cmap, unsupported_leaves=validation.unsupported_leaves
        )

    is_ok = validation.valid and not open_list

    if json_out:
        payload = {
            "case_id": case.case_id,
            "valid": validation.valid,
            "top_goal_id": validation.top_goal_id,
            "errors": validation.errors,
            "unsupported_leaves": validation.unsupported_leaves,
            "currency": currency_summary,
            "currency_drift": currency_drift,
            "open_defeaters": [
                {"id": d.id, "target_node_id": d.target_node_id, "statement": d.statement}
                for d in open_list
            ],
            "defeated_nodes": defeated,
            "receipt": receipt,
            "ok": is_ok,
        }
        print(json.dumps(payload, indent=2))
        raise typer.Exit(0 if is_ok else 1)

    # --- rich output ---
    console.print(f"[bold]Assurance case:[/bold] {case.case_id}  ({len(case.nodes)} nodes)")
    console.print(
        f"Structure: {'[green]VALID[/green]' if validation.valid else '[red]INVALID[/red]'}"
        f"   top goal: {validation.top_goal_id or '[red](none)[/red]'}"
    )
    for err in validation.errors:
        console.print(f"  [red]error:[/red] {err}")
    for leaf in validation.unsupported_leaves:
        console.print(f"  [yellow]unsupported leaf:[/yellow] {leaf}")

    if currency_summary:
        console.print(f"[bold]Currency[/bold] (as-of {as_of}):")
        for nid, status in sorted(currency_summary.items()):
            colour = {"current": "green", "due": "yellow", "overdue": "red"}.get(status, "white")
            console.print(f"  {nid}: [{colour}]{status}[/{colour}]")

    if open_list:
        console.print("[bold red]Open defeaters[/bold red] (argument defeated):")
        for d in open_list:
            console.print(f"  {d.id} → node {d.target_node_id}: {d.statement}")

    if receipt is not None:
        console.print(
            f"[bold]Conformance receipt[/bold]: {len(receipt['observations'])} observation(s), "
            f"{len(receipt['gaps'])} gap(s) across {', '.join(receipt['standards']) or '—'}"
        )

    raise typer.Exit(0 if is_ok else 1)
