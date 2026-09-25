"""nova export-model-independence — model-validation-independence evidence (ADR-0159 D2 / NF-276).

Read-only. Reads the shipped ADR-0058 maker-checker records for a model — the registry
``promotion_proposals`` written by ``nova promote propose/approve`` and, for any ``--capsule-id``,
the DSSE bundles written by ``nova seal propose/approve`` (outcome taken from the shipped
``verify_sod``) — and renders whether the validator identity differs from the developer identity.
It asserts only that independence was *recorded*, never that validation was *sufficient*; there is
no rating. A single-identity approval, an open proposal, a bypass, or an unverifiable record is
reported as ``missing`` with the reason — never fabricated.

Exit codes: 0 — rendered (including ``missing`` fields); 2 — a store is unreadable/corrupt or the
model id is malformed.
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
_SEAL_HOME = Path.home() / ".local" / "share" / "novafabric"


def export_model_independence_cmd(
    model: Annotated[
        str,
        typer.Option("--model", help="Model/asset id: name or name@version."),
    ],
    capsule_ids: Annotated[
        list[str] | None,
        typer.Option(
            "--capsule-id",
            help="Also read the NovaSeal DSSE maker-checker bundle for this capsule (repeatable).",
        ),
    ] = None,
    db: Annotated[
        Path | None,
        typer.Option(
            "--db",
            help="Registry database (default: $NOVAFABRIC_DB_PATH or "
            "$NOVAFABRIC_HOME/registry.db).",
        ),
    ] = None,
    data_dir: Annotated[
        Path,
        typer.Option("--data-dir", help="NovaSeal promote-bundle data directory."),
    ] = _SEAL_HOME,
    policy_db: Annotated[
        Path,
        typer.Option("--policy-db", help="NovaSeal promote-policy SQLite database."),
    ] = _SEAL_HOME / "merkle.db",
    json_out: Annotated[
        bool,
        typer.Option("--json", help="Emit the independence file as JSON."),
    ] = False,
) -> None:
    """Render ADR-0058 maker-checker validator-vs-developer independence evidence.

    Asserts only that independence was recorded — never that validation was
    sufficient, never a rating or compliance determination.

    \b
    Examples:
      nova export-model-independence --model credit-scorer@2.1
      nova export-model-independence --model credit-scorer --json
      nova export-model-independence --model credit-scorer --capsule-id <capsule-id>
    """
    from novafabric._paths import registry_db_path
    from novafabric.compliance.export.finance.model_independence import (
        MakerCheckerRecord,
        build_model_independence_file,
    )
    from novafabric.compliance.export.finance.model_independence_collect import (
        MakerCheckerSourceError,
        collect_registry_records,
        collect_seal_record,
    )

    records: list[MakerCheckerRecord] = []
    try:
        records.extend(collect_registry_records(db or registry_db_path(), model))
        for capsule_id in capsule_ids or []:
            rec = collect_seal_record(capsule_id, data_dir=data_dir, policy_db=policy_db)
            if rec is not None:
                records.append(rec)
    except ValueError as exc:
        err_console.print(f"[red]Invalid model id:[/red] {escape(str(exc))}")
        raise typer.Exit(2) from exc
    except MakerCheckerSourceError as exc:
        err_console.print(f"[red]Unreadable maker-checker record:[/red] {escape(str(exc))}")
        raise typer.Exit(2) from exc

    mif = build_model_independence_file(model_id=model, records=records)

    if json_out:
        print(json.dumps(mif.model_dump(mode="json"), indent=2))
        raise typer.Exit(0)

    ind = mif.independence
    s = mif.summary
    # regime in parens, not "[...]", which rich would parse as markup.
    console.print(
        f"Model-validation independence ({escape(mif.regime)})  model: {escape(mif.model_id)}   "
        f"records complete={s['complete']} missing={s['missing']}"
    )
    reason = f"  ({escape(ind.reason)})" if ind.reason else ""
    console.print(f"  {_MARK[ind.status]} {'independence':<24} {ind.status}{reason}")
    for r in mif.records:
        rec = r.record
        who = f"maker={rec.maker or '-'} checker={rec.checker or '-'}"
        why = f"  ({escape(r.reason)})" if r.reason else ""
        console.print(
            f"    {_MARK[r.status]} {escape(rec.record_ref)}  {escape(rec.subject)}  "
            f"{escape(who)}{why}"
        )
    console.print(f"[dim]{escape(mif.banner)}[/dim]")
    raise typer.Exit(0)
