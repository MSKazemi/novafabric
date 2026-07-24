"""nova audit-log — SIEM egress for local audit logs (ADR-0191, experimental).

``nova audit-log export`` one-shot-exports the hash-chained audit log
(``--source audit``) or the dashboard mutation log (``--source dashboard``)
over a time window, in ``jsonl`` (native), ``ocsf`` or ``cef`` format, to
stdout or a file. Every line passes the deny-by-default redaction pipeline;
the first line is a manifest recording the redaction-ruleset versions (a
JSON line for the JSON formats, a CEF event for ``cef``, so that stream
stays pure CEF). Transport is the site shipper's job — there is no network
sender here.

Namespace note (ADR-0191 D1): ``nova audit`` is the compliance
control-mapping engine; the security-log surface deliberately lives under
the separate ``nova audit-log`` group.

Exit codes: 0 = exported OK; 2 = bad parameters; 3 = chain verification
failed (the export is still written — tamper evidence must be
distinguishable, not suppressed).
"""

from __future__ import annotations

import sys
from collections.abc import Callable
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
            help="Output format: 'jsonl' (native, zero-mapping-loss), "
            "'ocsf' (OCSF classes; unmapped fields preserved verbatim) or "
            "'cef' (ArcSight CEF:0 for legacy collectors).",
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
    """Export an audit source over a time window, redacted, one entry per line.

    For the chained source the whole chain is verified during the walk;
    verification failure exits 3 but the export is still written, so
    pipelines can alert on the tamper evidence itself.

    \b
    Examples:
      nova audit-log export --source audit --format ocsf --out audit.ocsf.jsonl
      nova audit-log export --source audit --format cef --out audit.cef
      nova audit-log export --source dashboard --since 2026-07-01T00:00:00Z
    """
    from novafabric.audit.siem_export import (
        KNOWN_SOURCES,
        SiemExportError,
        export_entries,
    )

    if source not in KNOWN_SOURCES:
        supported = " or ".join(f"'{s}'" for s in sorted(KNOWN_SOURCES))
        raise typer.BadParameter(
            f"--source must be {supported}. Server route events are written "
            "to the dashboard log, so 'dashboard' already covers them; there "
            "is no separate 'server' source (OQ-038, resolved)."
        )
    if fmt not in ("jsonl", "ocsf", "cef"):
        raise typer.BadParameter("--format must be 'jsonl', 'ocsf' or 'cef'")

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


@app.command("tail")
def tail_cmd(
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
            help="Output format: 'jsonl' (native, zero-mapping-loss), "
            "'ocsf' (OCSF classes; unmapped fields preserved verbatim) or "
            "'cef' (ArcSight CEF:0 for legacy collectors).",
        ),
    ] = "jsonl",
    follow: Annotated[
        bool,
        typer.Option(
            "--follow/--no-follow",
            "-f",
            help="Keep running and render new entries as they are written.",
        ),
    ] = False,
    from_start: Annotated[
        bool,
        typer.Option(
            "--from-start",
            help="Replay the existing log first, then follow (default: start "
            "at the end, like tail).",
        ),
    ] = False,
    poll_interval: Annotated[
        float,
        typer.Option(
            "--poll-interval",
            help="Seconds to wait between polls when the log is idle.",
        ),
    ] = 1.0,
    out: Annotated[
        Optional[Path],
        typer.Option(
            "--out",
            help="Write to a size-bounded rotating file instead of stdout, "
            "for a file shipper to pick up.",
            show_default=False,
        ),
    ] = None,
    max_bytes: Annotated[
        int,
        typer.Option(
            "--max-bytes",
            help="Rotate --out at this size.",
        ),
    ] = 10 * 1024 * 1024,
    backup_count: Annotated[
        int,
        typer.Option(
            "--backup-count",
            help="Rotated generations to keep (0 = truncate, keep none).",
        ),
    ] = 5,
    syslog: Annotated[
        Optional[str],
        typer.Option(
            "--syslog",
            help="Send RFC 5424 messages to a LOCAL syslog endpoint: a unix "
            "socket path (/dev/log) or a loopback host:port. No default.",
            show_default=False,
        ),
    ] = None,
    syslog_transport: Annotated[
        str,
        typer.Option(
            "--syslog-transport",
            help="Transport for --syslog: 'auto' (unix for a path, else udp), "
            "'unix', 'udp', or 'tcp'.",
        ),
    ] = "auto",
) -> None:
    """Stream audit entries to stdout as they are written (experimental).

    A foreground process you run — a systemd unit or sidecar — not a
    NovaFabric-managed daemon. There is no network sink and no default
    endpoint: pipe stdout into your own shipper.

    Rotation and in-place truncation of the log are detected and followed.
    Continuity *across* a rotation cannot be verified (the predecessor's
    hash is gone with the old file), so a restart is reported rather than
    passed off as an unbroken chain.

    \b
    Examples:
      nova audit-log tail --follow | your-shipper
      nova audit-log tail --follow --format cef --source dashboard
      nova audit-log tail --from-start --format ocsf
    """
    from novafabric.audit.siem_export import (
        KNOWN_SOURCES,
        SiemExportError,
        follow_entries,
    )
    from novafabric.audit.sinks import RotatingFileSink, SinkError, SyslogSink

    if source not in KNOWN_SOURCES:
        supported = " or ".join(f"'{s}'" for s in sorted(KNOWN_SOURCES))
        raise typer.BadParameter(f"--source must be {supported}")
    if fmt not in ("jsonl", "ocsf", "cef"):
        raise typer.BadParameter("--format must be 'jsonl', 'ocsf' or 'cef'")
    if out is not None and syslog is not None:
        raise typer.BadParameter(
            "--out and --syslog are alternative sinks; pass at most one"
        )

    # Without --follow this is a bounded single pass, so the loop must end
    # after one drain rather than block forever on an idle log.
    stop = None if follow else _once()

    sink: RotatingFileSink | SyslogSink | None = None
    try:
        if out is not None:
            sink = RotatingFileSink(
                out, max_bytes=max_bytes, backup_count=backup_count
            )
        elif syslog is not None:
            sink = SyslogSink(syslog, transport=syslog_transport, msgid=f"audit-{fmt}")
    except SinkError as exc:
        err_console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=2) from exc

    try:
        result = follow_entries(
            source=source,
            fmt=fmt,
            out=sink if sink is not None else sys.stdout,
            from_start=from_start or not follow,
            poll_interval=poll_interval,
            stop=stop,
        )
    except SiemExportError as exc:
        err_console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=2) from exc
    finally:
        if sink is not None:
            sink.close()

    err_console.print(
        f"[dim]streamed {result.entries_emitted} entr"
        f"{'y' if result.entries_emitted == 1 else 'ies'} "
        f"(source={source}, format={fmt}"
        f"{f', {result.rotations} rotation(s)' if result.rotations else ''})[/dim]"
    )

    if result.chain_errors:
        for error in result.chain_errors:
            err_console.print(f"[red]✗ chain[/red] {error}")
        err_console.print(
            f"[red]✗ Chain verification FAILED ({result.chain_error_count} "
            "error(s)) — treat the stream above as tamper-evidence[/red]"
        )
        raise typer.Exit(code=3)


def _once() -> Callable[[], bool]:
    """A ``stop`` predicate that ends the follow loop after one drain."""
    calls = 0

    def _stop() -> bool:
        nonlocal calls
        calls += 1
        return calls > 1

    return _stop
