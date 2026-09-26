"""nova export-cat — CAT-style lifecycle agent-event trail (ADR-0159 D6 / NF-280).

Read-only and offline. Reads one sealed Run Capsule (by run id or path) and renders a
lifecycle-ordered, self-contained agent-event trail — lifecycle stage, actor identity ref, and
timestamps per recorded event — as a firm-owned artifact modelled on the SEC Rule 613 CAT event
lifecycle. Stages: origination (run start), decision (model calls), authorization (tool-permission
decisions, human approvals), action (tool calls), disposition (run finish). A missing required
stage makes the trail ``partial``; no event is ever synthesised.

It is evidence of what was recorded — not a CAT submission. Nothing is transmitted; no network
connection is opened.

Exit codes: 0 — rendered (including ``missing`` / ``partial`` stages); 2 — the capsule cannot be
found, its sealed evidence is unreadable / malformed / does not match its recorded digests, or
``--out`` points inside the capsule.
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


def export_cat_cmd(
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
        typer.Option("--json", help="Emit the event trail as JSON on stdout."),
    ] = False,
    out: Annotated[
        Path | None,
        typer.Option("--out", help="Also write the event trail JSON to this file."),
    ] = None,
) -> None:
    """Render a CAT-style lifecycle-ordered agent-event trail from a sealed capsule.

    Firm-owned, self-hosted evidence modelled on the SEC Rule 613 CAT event
    lifecycle — NOT a CAT submission. Never connects to the CAT central
    repository; nothing is transmitted. A missing lifecycle stage makes the
    trail partial; no event is ever synthesised.

    \b
    Examples:
      nova export-cat --run-id 01KZ...
      nova export-cat --run-id 01KZ... --json
      nova export-cat --run-id 01KZ... --out cat-events.json
    """
    from novafabric.cli._capsule_ref import CapsuleRefError, resolve_capsule_ref
    from novafabric.compliance.export.finance.cat_collect import (
        CorruptCapsuleError,
        collect_trail_facts,
    )
    from novafabric.compliance.export.finance.cat_trail import build_cat_trail

    try:
        cap = resolve_capsule_ref(run_id, capsule_dir=capsule_dir)
    except CapsuleRefError as exc:
        err_console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(2) from exc
    if out is not None and out.resolve().is_relative_to(cap.resolve()):
        err_console.print("[red]--out must not point inside the capsule (read-only export).[/red]")
        raise typer.Exit(2)
    try:
        facts = collect_trail_facts(cap)
    except CorruptCapsuleError as exc:
        err_console.print(f"[red]Corrupt or unreadable capsule evidence:[/red] {escape(str(exc))}")
        raise typer.Exit(2) from exc

    trail = build_cat_trail(facts)
    payload = json.dumps(trail.model_dump(mode="json"), indent=2, sort_keys=True)
    if out is not None:
        try:
            out.write_text(payload + "\n", encoding="utf-8")
        except OSError as exc:
            err_console.print(f"[red]Cannot write --out:[/red] {escape(str(exc))}")
            raise typer.Exit(2) from exc

    if json_out:
        print(payload)
        raise typer.Exit(0)

    s = trail.summary
    console.print(
        f"CAT-style agent-event trail  run: {escape(trail.run_id)}  "
        f"trail={trail.trail_status}  events={len(trail.events)}  "
        f"complete={s['complete']} partial={s['partial']} missing={s['missing']}"
    )
    console.print(f"[dim]{escape(trail.regime)}[/dim]")
    for row in trail.rows:
        why = f"  ({escape('; '.join(row.reasons))})" if row.reasons else ""
        req = "" if row.required else " (optional)"
        console.print(
            f"  {_MARK[row.status]} {row.key:<14} {row.status} events={row.event_count}{req}{why}"
        )
    for ev in trail.events:
        console.print(
            f"    #{ev.sequence} {escape(ev.timestamp or '(no timestamp)')}  {ev.stage}  "
            f"{escape(ev.event_type)}  actor={escape(ev.actor_ref or '-')}  "
            f"outcome={escape(ev.outcome or '-')}  ref={escape(ev.source_ref)}"
        )
    if out is not None:
        console.print(f"Wrote {escape(str(out))}")
    console.print(f"[bold]{escape(trail.cat_honesty)}[/bold]")
    console.print(f"[dim]{escape(trail.banner)}[/dim]")
    raise typer.Exit(0)
