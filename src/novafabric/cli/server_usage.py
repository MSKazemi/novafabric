"""CLI command group: `nova server usage` (ADR-0208 P2, experimental).

Subcommands:
  nova server usage reconcile — Report ledger↔capsule-store drift; --apply
                                appends a signed adjustment row (default
                                workspace only).
  nova server usage export    — Chargeback export of per-workspace usage as
                                RFC 4180 CSV or NDJSON.

Both read the registry SQLite DB the server meters into (``--db-path``, else
the server config's ``db_path``, else the registry default) and honor the
server config's ``server.usage`` retention keys (``--config``).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Optional

import typer

if TYPE_CHECKING:
    from novafabric.server.config import ServerConfig

usage_app = typer.Typer(
    name="usage",
    help=(
        "Usage-metering admin: drift reconciliation and chargeback export (experimental, ADR-0208)."
    ),
    no_args_is_help=True,
)

_CONFIG_HELP = "Server YAML config path. Defaults to ~/.config/novafabric/server.yaml."
_DB_HELP = "SQLite DB path. Defaults to the server config's db_path, else the registry default."


def _load(
    config: Optional[Path],  # noqa: UP007
    db_path: Optional[Path],  # noqa: UP007
) -> tuple[ServerConfig, Path | None]:
    """Load the server config (exit 2 on error) and resolve the DB path."""
    from novafabric.server.config import load_config

    try:
        cfg = load_config(config)
    except Exception as exc:  # noqa: BLE001 — surfaced as a CLI error
        typer.echo(f"Invalid server config: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    resolved = db_path or (Path(cfg.db_path) if cfg.db_path else None)
    return cfg, resolved


@usage_app.command("reconcile")
def usage_reconcile_cmd(
    apply: Annotated[
        bool,
        typer.Option(
            "--apply",
            help=(
                "Append a signed reconciliation adjustment row for the default "
                "workspace. Without it the command only reports."
            ),
        ),
    ] = False,
    capsule_dir: Annotated[
        Optional[Path],  # noqa: UP007
        typer.Option("--capsule-dir", help="Capsule store to measure. Defaults to the server's."),
    ] = None,
    actor: Annotated[
        str,
        typer.Option("--actor", help="Actor recorded on the ledger row and audit entry."),
    ] = "cli",
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Emit the result as JSON for tooling."),
    ] = False,
    config: Annotated[
        Optional[Path],  # noqa: UP007
        typer.Option("--config", help=_CONFIG_HELP),
    ] = None,
    db_path: Annotated[
        Optional[Path],  # noqa: UP007
        typer.Option("--db-path", help=_DB_HELP),
    ] = None,
) -> None:
    """Compare metered usage with the capsule store and report drift (ADR-0208).

    Report-only by default: drift = derived (capsule store) minus metered
    (ledger). With --apply and non-zero drift, appends one signed (+/-)
    adjustment row per drifting metric to the DEFAULT workspace only
    (attribution 'reconciliation', ref 'recon:<timestamp>'); existing rows
    and counters are never rewritten. Every run appends a 'usage.reconcile'
    entry to the hash-chained audit log. Scope: single server. Experimental.

    \b
    Exit codes: 0 ok · 1 refused / audit failure · 2 invalid config.

    \b
    Examples:
      nova server usage reconcile
      nova server usage reconcile --json
      nova server usage reconcile --apply --actor alice@example.com
    """
    from novafabric._paths import default_capsule_dir
    from novafabric.server.usage_reconcile import ReconciliationError, reconcile

    cfg, resolved_db = _load(config, db_path)
    try:
        result = reconcile(
            capsule_dir or default_capsule_dir(),
            apply=apply,
            actor=actor,
            db_path=resolved_db,
            rollup_retention_months=cfg.usage.rollup_retention_months,
            ledger_retention_months=cfg.usage.ledger_retention_months,
        )
    except ReconciliationError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    if as_json:
        typer.echo(result.model_dump_json(indent=2))
        return

    rep = result.report
    typer.echo(f"Capsule store: {rep.capsule_dir}")
    typer.echo(f"  derived : {rep.derived_capsules} capsules, {rep.derived_bytes} bytes")
    typer.echo(f"  metered : {rep.metered_capsules} capsules, {rep.metered_bytes} bytes")
    typer.echo(f"  drift   : {rep.drift_capsules:+d} capsules, {rep.drift_bytes:+d} bytes")
    if rep.in_sync:
        typer.echo("In sync — no adjustment needed.")
    elif result.applied:
        typer.echo(
            f"Applied: {result.rows_recorded} adjustment row(s) for workspace "
            f"'{result.workspace}' (ref {result.ref})."
        )
    else:
        typer.echo("Report only — re-run with --apply to append an adjustment row.")
    if not result.audited:
        typer.echo("Warning: the audit entry could not be appended.", err=True)


@usage_app.command("export")
def usage_export_cmd(
    period_from: Annotated[
        Optional[str],  # noqa: UP007
        typer.Option("--from", help="First period YYYY-MM (default: current UTC period)."),
    ] = None,
    period_to: Annotated[
        Optional[str],  # noqa: UP007
        typer.Option("--to", help="Last period YYYY-MM, inclusive (default: --from)."),
    ] = None,
    fmt: Annotated[
        str,
        typer.Option("--format", help="Output format: csv (RFC 4180) or ndjson."),
    ] = "csv",
    workspace: Annotated[
        Optional[str],  # noqa: UP007
        typer.Option("--workspace", help="Only this workspace slug."),
    ] = None,
    org: Annotated[
        Optional[str],  # noqa: UP007
        typer.Option("--org", help="Only this org slug."),
    ] = None,
    output: Annotated[
        Optional[Path],  # noqa: UP007
        typer.Option("--output", "-o", help="Write to this file instead of stdout."),
    ] = None,
    config: Annotated[
        Optional[Path],  # noqa: UP007
        typer.Option("--config", help=_CONFIG_HELP),
    ] = None,
    db_path: Annotated[
        Optional[Path],  # noqa: UP007
        typer.Option("--db-path", help=_DB_HELP),
    ] = None,
) -> None:
    """Export per-workspace usage for chargeback as CSV or NDJSON (ADR-0208).

    One row per (period, org, workspace, metric), sorted in that order.
    Finalized periods come from the monthly rollups (status 'final');
    not-yet-finalized periods, always including the current one, from the
    live counters (status 'provisional'). CSV is RFC 4180 with
    formula-injection-safe text cells. Read-only. Scope: single server.
    Experimental.

    \b
    Examples:
      nova server usage export --from 2026-07 --to 2026-09
      nova server usage export --from 2026-08 --format ndjson --workspace ml-team
      nova server usage export --from 2026-08 -o chargeback-2026-08.csv
    """
    from novafabric.server.usage import period_for
    from novafabric.server.usage_export import (
        ChargebackExportError,
        chargeback_rows,
        render,
    )

    if fmt not in ("csv", "ndjson"):
        typer.echo(f"Invalid --format '{fmt}'. Choose from: csv, ndjson", err=True)
        raise typer.Exit(code=2)
    _cfg, resolved_db = _load(config, db_path)
    start = period_from or period_for()
    end = period_to or start
    try:
        rows = chargeback_rows(start, end, db_path=resolved_db, workspace=workspace, org=org)
    except ChargebackExportError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc

    text = render(rows, "csv" if fmt == "csv" else "ndjson")
    if output is None:
        typer.echo(text, nl=False)
        return
    output.write_text(text, encoding="utf-8", newline="")
    typer.echo(f"Wrote {len(rows)} row(s) to {output}.", err=True)
