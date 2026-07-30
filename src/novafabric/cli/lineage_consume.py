"""``nova lineage consume`` — run the NATS JetStream LineageConsumer as a
foreground daemon (cap-006, ADR-0061/ADR-0066/ADR-0219, experimental).

Before this command existed, ``LineageConsumer.run_from_nats()`` had no
deployable entrypoint anywhere in the CLI — it was only reachable from Python
code or tests, even though the consumer itself (extraction, cross-batch dedup,
and the KuzuDB bulk-COPY write path) was fully implemented and tested.
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Optional

import typer
from rich.console import Console

console = Console()


def consume_cmd(
    nats_url: Annotated[
        Optional[str],
        typer.Option("--nats-url", help="NATS URL (default: $NOVA_NATS_URL)"),
    ] = None,
    kuzu_path: Annotated[
        Optional[str],
        typer.Option(
            "--kuzu-path",
            help="KuzuDB directory to write lineage edges into "
            "(default: $NOVA_KUZU_PATH or .nova/kg/lineage.kuzu)",
        ),
    ] = None,
    subject: Annotated[
        str, typer.Option("--subject", help="NATS subject pattern to subscribe to")
    ] = "novafabric.lineage.>",
    batch_size: Annotated[
        int, typer.Option("--batch-size", help="Max messages pulled per NATS fetch")
    ] = 500,
    fetch_timeout: Annotated[
        float, typer.Option("--fetch-timeout", help="Seconds to wait per NATS fetch")
    ] = 1.0,
    flush_batch_size: Annotated[
        int,
        typer.Option(
            "--flush-batch-size",
            help="Edges to accumulate before a KuzuDB COPY flush "
            "(2,000+ recommended — see bench/lineage/MEASURED_CEILING.md)",
        ),
    ] = 2000,
    flush_interval_s: Annotated[
        float,
        typer.Option(
            "--flush-interval-s",
            help="Max seconds between KuzuDB flushes even below --flush-batch-size",
        ),
    ] = 15.0,
) -> None:
    """Run the LineageConsumer NATS JetStream pull consumer in the foreground.

    Pulls capsule events from NATS JetStream, derives lineage edges, and
    bulk-COPYs them into KuzuDB on a size-or-time flush trigger. Runs until
    interrupted (Ctrl-C). Requires the scale extra (nats-py) and the
    scale-kg extra (kuzu) — see pyproject.toml.

    Scope: cluster-scale deployment (long-running daemon, not local-mode).

    \b
    Examples:
      nova lineage consume
      nova lineage consume --nats-url nats://hub.example.com:4222 \\
        --kuzu-path /var/lib/novafabric/lineage.kuzu
      nova lineage consume --flush-batch-size 5000 --flush-interval-s 30
    """
    from novafabric.lineage.consumer import LineageConsumer

    consumer = LineageConsumer(nats_url=nats_url, kuzu_path=kuzu_path)
    console.print(
        f"[dim]nova lineage consume: nats={consumer.nats_url} "
        f"kuzu={consumer.kuzu_path} subject={subject!r}[/dim]"
    )
    try:
        asyncio.run(
            consumer.run_from_nats(
                subject=subject,
                batch_size=batch_size,
                fetch_timeout=fetch_timeout,
                flush_batch_size=flush_batch_size,
                flush_interval_s=flush_interval_s,
            )
        )
    except KeyboardInterrupt:
        pass
    finally:
        console.print("\n[dim]nova lineage consume stopped.[/dim]")
