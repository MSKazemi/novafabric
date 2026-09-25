"""nova export-retention — financial-record retention-posture attestation (ADR-0159 D5 / NF-277).

Read-only. For one or more Evidence Bundles it attests the ADR-0031 retention posture — the
registry ``retention-policy.yaml`` window and legal-hold state, each run's WORM lock (local
adapter or supplied S3/Azure/GCS receipt), the RFC 3161 trusted timestamp **as actually recorded
in each bundle** (ADR-0030, shipped), and the hash-chained audit trail — each complete / partial /
missing with facts or a reason. It attests posture, never compliance, and makes nothing immutable.

Exit codes: 0 — rendered (including ``missing`` fields); 2 — a bundle or retention source is
unreadable/corrupt.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.markup import escape

console = Console()
err_console = Console(stderr=True)

_MARK = {"complete": "[green]✓[/green]", "partial": "[yellow]◐[/yellow]", "missing": "[red]✗[/red]"}
_REGISTRIES = Path(".novafabric") / "registries"


class RegimeOption(str, Enum):
    """CLI mirror of ``compliance.export.finance.retention.Regime`` (kept import-light;
    a test pins the two value sets equal)."""

    sec_17a_4 = "17a-4"
    mifid = "mifid"


def export_retention_cmd(
    bundles: Annotated[
        list[Path],
        typer.Option("--bundle", help="Evidence Bundle ZIP (repeatable)."),
    ],
    regime: Annotated[
        RegimeOption,
        typer.Option("--regime", help="Regime tag rendered on the attestation."),
    ] = RegimeOption.sec_17a_4,
    registry: Annotated[
        str | None,
        typer.Option(
            "--registry",
            help="Registry whose retention-policy.yaml, holds.jsonl and worm.db are read.",
        ),
    ] = None,
    worm_db: Annotated[
        Path | None,
        typer.Option("--worm-db", help="Local WORM adapter DB (default: the registry's worm.db)."),
    ] = None,
    worm_receipts: Annotated[
        Path | None,
        typer.Option(
            "--worm-receipts",
            help="JSON WormReceipt object/list returned by an S3/Azure/GCS WORM put.",
        ),
    ] = None,
    audit_log: Annotated[
        Path | None,
        typer.Option(
            "--audit-log", help="Hash-chained audit log (default: the NovaFabric audit.jsonl)."
        ),
    ] = None,
    json_out: Annotated[
        bool,
        typer.Option("--json", help="Emit the attestation as JSON."),
    ] = False,
) -> None:
    """Attest WORM / retention / legal-hold / RFC 3161 posture over Evidence Bundles.

    Reports the trusted timestamp when a bundle carries one and missing-with-reason
    only when it genuinely does not. Attests posture — never compliance.

    \b
    Examples:
      nova export-retention --bundle evidence.zip --registry prod
      nova export-retention --bundle a.zip --bundle b.zip --regime mifid --json
      nova export-retention --bundle a.zip --worm-receipts receipts.json
    """
    from novafabric.audit import AUDIT_LOG_PATH
    from novafabric.compliance.export.finance.retention import (
        Regime,
        build_retention_attestation,
    )
    from novafabric.compliance.export.finance.retention_collect import (
        CorruptEvidenceError,
        collect_bundle_facts,
        read_holds,
        read_policy,
    )

    registry_dir = _REGISTRIES / registry if registry is not None else None
    if worm_db is None and registry_dir is not None:
        worm_db = registry_dir / "worm.db"
    try:
        policy = read_policy(registry_dir) if registry_dir is not None else None
        holds = read_holds(registry_dir) if registry_dir is not None else None
        facts = collect_bundle_facts(
            bundles,
            worm_db=worm_db,
            worm_receipts=worm_receipts,
            audit_log=audit_log or AUDIT_LOG_PATH,
        )
    except CorruptEvidenceError as exc:
        err_console.print(f"[red]Unreadable evidence:[/red] {escape(str(exc))}")
        raise typer.Exit(2) from exc

    att = build_retention_attestation(
        regime=Regime(regime.value),
        as_of=datetime.now(timezone.utc),
        registry=registry,
        policy=policy,
        holds=holds,
        bundles=facts,
    )

    if json_out:
        print(json.dumps(att.model_dump(mode="json"), indent=2))
        raise typer.Exit(0)

    s = att.summary
    console.print(
        f"Retention posture ({escape(att.regime)})  registry: {escape(att.registry or '-')}   "
        f"complete={s['complete']} partial={s['partial']} missing={s['missing']}"
    )
    for row in att.posture:
        console.print(_line(row.element, row.status, row.reason, indent="  "))
    for art in att.artifacts:
        console.print(f"  bundle {escape(art.bundle)}  run: {escape(art.run_id)}")
        for row in art.rows:
            console.print(_line(row.element, row.status, row.reason, indent="    "))
    console.print(f"[dim]{escape(att.banner)}[/dim]")
    raise typer.Exit(0)


def _line(element: str, status: str, reason: str | None, *, indent: str) -> str:
    """One rich-markup row: mark, element, status and (escaped) reason."""
    why = f"  ({escape(reason)})" if reason else ""
    return f"{indent}{_MARK.get(status, '?')} {element:<20} {status}{why}"
