"""``nova support-bundle`` — redacted diagnostics tarball (ADR-0187, experimental)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console

from novafabric.support_bundle import build_support_bundle

console = Console()


def support_bundle_cmd(
    output: Annotated[
        Optional[Path],
        typer.Option(
            "--output",
            "-o",
            help="Output tarball path (default: ./nova-support-bundle-<timestamp>.tar.gz).",
            show_default=False,
        ),
    ] = None,
    log_window_hours: Annotated[
        int,
        typer.Option(
            "--log-window-hours",
            help=(
                "Collect only *.log files modified within this many hours (from "
                "NOVAFABRIC_LOG_DIR or $NOVAFABRIC_HOME/logs); recorded in the manifest."
            ),
        ),
    ] = 24,
) -> None:
    """Write a redacted diagnostics tarball for support (experimental, ADR-0187).

    Contains ONLY allowlisted members: MANIFEST.txt (what each file is and
    why it is there), doctor.json, versions.json, env.txt
    (NOVAFABRIC_*/NOVA_* variable names only — never values), health.json,
    config.redacted.yaml (if a server config exists, with secret-keyed
    values redacted), the redacted tails of recent logs/*.log files (only if
    you keep logs in NOVAFABRIC_LOG_DIR or $NOVAFABRIC_HOME/logs), and a
    manifest.json with the SHA-256 of every member plus the rule versions.
    No collector reads tokens, keys, capsule payloads, prompts or responses.
    Your home directory becomes ~, your user name after a home, scratch or
    tmp directory becomes [USER], and this host's name as a whole word
    becomes [HOSTNAME] (versions.json is left as is).

    The archive stays on this computer; nothing is uploaded. Redaction is
    pattern-based, so review the files (log text especially) before you
    share it.

    Scope: global (snapshots the whole installation).

    \b
    Examples:
      # Default output name in the current directory
      nova support-bundle

      # Explicit output path
      nova support-bundle -o /tmp/diag.tar.gz
    """
    if output is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        output = Path(f"nova-support-bundle-{stamp}.tar.gz")

    try:
        result = build_support_bundle(output, log_window_hours=log_window_hours)
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1) from exc

    console.print(f"[green]Support bundle written:[/green] {result.path}")
    console.print("[bold]Members[/bold]")
    for name in result.members:
        console.print(f"  - {name}")
    console.print(f"[bold]Manifest SHA-256:[/bold] {result.manifest_sha256}")
    console.print(
        "[yellow]Review before sharing:[/yellow] MANIFEST.txt inside the archive says "
        "what each file is. Redaction is pattern-based; read any logs/ files yourself. "
        "Nothing was uploaded."
    )
