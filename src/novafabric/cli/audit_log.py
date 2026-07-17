"""nova audit-log — SIEM egress for local audit logs (ADR-0191, experimental).

``nova audit-log export`` one-shot-exports the hash-chained audit log
(``--source audit``) or the dashboard mutation log (``--source dashboard``)
over a time window, in ``jsonl`` (native) or ``ocsf`` format, to stdout or a
file. Every line passes the deny-by-default redaction pipeline; the first
line is a header entry recording the redaction-ruleset versions. Transport
is the site shipper's job — there is no network sender here.

Namespace note (ADR-0191 D1): ``nova audit`` is the compliance
control-mapping engine; the security-log surface deliberately lives under
the separate ``nova audit-log`` group.

Exit codes: 0 = exported OK; 2 = bad parameters; 3 = chain verification
failed (the export is still written — tamper evidence must be
distinguishable, not suppressed).
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console

app = typer.Typer(
    name="audit-log",
    help=(
        "Export local audit logs for SIEM ingestion: OCSF or native JSONL "
        "(experimental, ADR-0191). The site's shipper does transport."
    ),
    no_args_is_help=True,
)

err_console = Console(stderr=True)


def _parse_cli_ts(value: str, flag: str) -> datetime:
    try:
        ts = datetime.fromisoformat(value)
    except ValueError as exc:
        raise typer.BadParameter(
            f"{flag} must be an ISO-8601 timestamp (got {value!r})"
        ) from exc
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


@app.command("export")
def export_cmd(
    source: Annotated[
        str,
        typer.Option(
            "--source",
            help="Audit source: 'audit' (hash-chained log) or 'dashboard' "
            "(nova serve mutation log).",
        ),
    ] = "audit",
    fmt: Annotated[
        str,
        typer.Option(
            "--format",
            help="Output format: 'jsonl' (native, zero-mapping-loss) or "
            "'ocsf' (OCSF classes; unmapped fields preserved verbatim).",
        ),
    ] = "jsonl",
    since: Annotated[
        Optional[str],
        typer.Option(
            "--since",
            help="Inclusive ISO-8601 lower bound (naive = UTC).",
            show_default=False,
        ),
    ] = None,
    until: Annotated[
        Optional[str],
        typer.Option(
            "--until",
            help="Exclusive ISO-8601 upper bound (naive = UTC).",
            show_default=False,
        ),
    ] = None,
    out: Annotated[
        Optional[Path],
        typer.Option(
            "--out",
            help="Output file (default: stdout).",
            show_default=False,
        ),
    ] = None,
) -> None:
    """Export an audit source over a time window, redacted, one JSON line each.

    For the chained source the whole chain is verified during the walk;
    verification failure exits 3 but the export is still written, so
    pipelines can alert on the tamper evidence itself.

    \b
    Examples:
      nova audit-log export --source audit --format ocsf --out audit.ocsf.jsonl
      nova audit-log export --source dashboard --since 2026-07-01T00:00:00Z
    """
    from novafabric.audit.siem_export import SiemExportError, export_entries

    if source not in ("audit", "dashboard"):
        raise typer.BadParameter(
            "--source must be 'audit' or 'dashboard' ('server' lands in a "
            "later slice, ADR-0191)"
        )
    if fmt not in ("jsonl", "ocsf"):
        raise typer.BadParameter(
            "--format must be 'jsonl' or 'ocsf' ('cef' lands in slice 2, "
            "ADR-0191)"
        )

    since_ts = _parse_cli_ts(since, "--since") if since else None
    until_ts = _parse_cli_ts(until, "--until") if until else None

    try:
        if out is not None:
            with out.open("w", encoding="utf-8") as fh:
                result = export_entries(
                    source=source, fmt=fmt, out=fh, since=since_ts, until=until_ts
                )
        else:
            result = export_entries(
                source=source, fmt=fmt, out=sys.stdout, since=since_ts, until=until_ts
            )
    except SiemExportError as exc:
        err_console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=2) from exc

    err_console.print(
        f"[dim]exported {result.entries_exported} entr"
        f"{'y' if result.entries_exported == 1 else 'ies'} "
        f"(source={source}, format={fmt})[/dim]"
    )

    if result.chain_errors:
        for error in result.chain_errors:
            err_console.print(f"[red]✗ chain[/red] {error}")
        err_console.print(
            "[red]✗ Chain verification FAILED — the export above is "
            "tamper-evidence, treat it accordingly[/red]"
        )
        raise typer.Exit(code=3)
