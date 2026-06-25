"""``nova collector`` — event-buffer operations (experimental, ADR-0020).

Productized from the SPK-COL spike outcomes (gap-002): offset-replay rebuild
of the durable JetStream buffer. Requires ``novafabric[scale]`` for the
NATS client.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console

console = Console()

collector_app = typer.Typer(
    help=(
        "Cluster collector buffer operations (experimental, ADR-0020): "
        "offset-replay rebuild of the durable event buffer."
    ),
    no_args_is_help=True,
)


@collector_app.callback()
def _collector_callback() -> None:
    """Event-buffer operations for the cluster collector (experimental)."""


@collector_app.command("rebuild")
def collector_rebuild_cmd(
    target: Annotated[
        Path, typer.Option("--target", help="Directory to materialize per-run JSONL into")
    ],
    nats_url: Annotated[
        Optional[str],
        typer.Option("--nats-url", help="Broker URL (default: $NOVA_NATS_URL)"),
    ] = None,
    stream: Annotated[
        Optional[str],
        typer.Option(
            "--stream",
            help="JetStream stream (default: $NOVA_NATS_STREAM or nova-evidence)",
        ),
    ] = None,
    subject: Annotated[
        Optional[str],
        typer.Option(
            "--subject",
            help="Subject filter (default: $NOVA_NATS_SUBJECT or nova.evidence.>)",
        ),
    ] = None,
    report_path: Annotated[
        Optional[Path],
        typer.Option("--report", help="Write the RebuildReport JSON here"),
    ] = None,
) -> None:
    """Rebuild the downstream store by replaying the buffer from offset 0.

    Replays every event in the durable JetStream buffer (run_id-keyed,
    SI-1) into per-run JSONL files, reporting per-run sha256 digests and
    seq-order preservation — the SPK-COL-1-proven rebuild path.

    Scope: event buffer → local directory. Requires novafabric[scale].

    \b
    Examples:
      nova collector rebuild --target ./rebuilt
      nova collector rebuild --target ./rebuilt --stream nova-evidence --report report.json
    """
    from novafabric.evidence_fabric.rebuild import OffsetReplayRebuilder, RebuildError

    rebuilder = OffsetReplayRebuilder(
        nats_url=nats_url, stream=stream, subject=subject
    )
    try:
        report = asyncio.run(rebuilder.rebuild(target))
    except RebuildError as exc:
        console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=1) from exc
    except OSError as exc:
        console.print(f"[red]✗[/red] broker unreachable: {exc}")
        raise typer.Exit(code=1) from exc

    console.print(
        f"[green]✓[/green] Replayed {report.events_replayed} event(s) into "
        f"{report.target_dir} ({len(report.runs)} run partition(s))"
    )
    for run in report.runs:
        flag = "" if run.order_preserved else "  [red]ORDER VIOLATION[/red]"
        console.print(
            f"  {run.run_id}: {run.events} events  sha256={run.sha256[:16]}…{flag}"
        )
    if report_path is not None:
        report_path.write_text(report.model_dump_json(indent=2) + "\n")
        console.print(f"Report written to {report_path}")
    if not report.order_preserved:
        raise typer.Exit(code=2)
