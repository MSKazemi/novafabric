"""nova kg — Capsule Knowledge Graph commands.

Requires novafabric[scale-kg] (kuzu>=0.11.3).
Commands work without kuzu installed — they emit a clear ImportError message.

Tier 2 (alias table) and Tier 3 (entity review queue) use SQLite and require
no extra dependencies.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Optional

if TYPE_CHECKING:
    from novafabric.kg.spkg.graph_store import SpkgGraphStore

import typer
from rich.console import Console
from rich.table import Table

console = Console()
logger = logging.getLogger(__name__)

_DEFAULT_KG_PATH = ".nova/kg/nova_kg.kuzu"
_DEFAULT_ALIAS_DB_PATH = ".nova/kg/alias.db"
_DEFAULT_QUEUE_DB_PATH = ".nova/kg/review_queue.db"

_KgPathArg = Annotated[
    str,
    typer.Option(
        "--path",
        envvar="NOVA_KG_PATH",
        help="KuzuDB path for the capsule knowledge graph.",
        show_default=True,
    ),
]

_AliasDBAr = Annotated[
    str,
    typer.Option(
        "--alias-db",
        envvar="NOVA_KG_ALIAS_DB",
        help="SQLite path for the Tier-2 alias table.",
        show_default=True,
    ),
]

_QueueDBAr = Annotated[
    str,
    typer.Option(
        "--queue-db",
        envvar="NOVA_KG_QUEUE_DB",
        help="SQLite path for the Tier-3 entity review queue.",
        show_default=True,
    ),
]

kg_app = typer.Typer(
    name="kg",
    help="Capsule Knowledge Graph: ingest, query, and inspect (requires novafabric[scale-kg]).",
    no_args_is_help=True,
)

# Sub-app for alias management (Tier 2)
alias_app = typer.Typer(
    name="alias",
    help="Manage the Tier-2 entity alias table (SQLite-backed).",
    no_args_is_help=True,
)
kg_app.add_typer(alias_app, name="alias")

# Sub-app for entity review queue (Tier 3)
queue_app = typer.Typer(
    name="entity-queue",
    help="Manage the Tier-3 entity human review queue (SQLite-backed).",
    no_args_is_help=True,
)
kg_app.add_typer(queue_app, name="entity-queue")


# ---------------------------------------------------------------------------
# nova kg init
# ---------------------------------------------------------------------------


@kg_app.command("init")
def kg_init_cmd(
    path: _KgPathArg = _DEFAULT_KG_PATH,
) -> None:
    """Initialise a new Capsule Knowledge Graph store.

    Creates the KuzuDB database and alias/review-queue SQLite tables.
    Safe to run multiple times — all DDL statements use IF NOT EXISTS.

    Scope: global (creates the KG store at --path).

    \b
    Examples:
      nova kg init
      nova kg init --path /custom/kg/store
    """
    try:
        from novafabric.kg.store import KGStore
    except ImportError as exc:
        console.print(f"[red]Error:[/red] {exc}", highlight=False)
        raise typer.Exit(1)

    store = KGStore(path)
    store.init_schema()
    console.print(f"[green]KG schema initialised at[/green] {path}")


# ---------------------------------------------------------------------------
# nova kg status
# ---------------------------------------------------------------------------


@kg_app.command("status")
def kg_status_cmd(
    path: _KgPathArg = _DEFAULT_KG_PATH,
) -> None:
    """Show KG store health and node/edge statistics.

    Scope: knowledge graph.

    \b
    Examples:
      nova kg status
      nova kg status --path /custom/kg/store
    """
    try:
        from novafabric.kg.store import KGStore
    except ImportError as exc:
        console.print(f"[red]Error:[/red] {exc}", highlight=False)
        raise typer.Exit(1)

    try:
        store = KGStore(path)
        status = store.get_status()
        health = status.get("store_health", "unknown")
        color = "green" if health == "ok" else "red"
        console.print(f"[{color}]KG store health:[/{color}] {health}")
        console.print(f"  db_path:    {status.get('db_path')}")
        console.print(f"  edge_count: {status.get('edge_count', 0)}")
        node_counts = status.get("node_counts", {})
        if node_counts:
            tbl = Table(title="Node counts per layer", show_header=True)
            tbl.add_column("Node type")
            tbl.add_column("Count", justify="right")
            for label, cnt in node_counts.items():
                tbl.add_row(label, str(cnt))
            console.print(tbl)
        if status.get("error"):
            console.print(f"[red]Error:[/red] {status['error']}", highlight=False)
            raise typer.Exit(1)
    except typer.Exit:
        raise
    except Exception as exc:
        console.print(f"[red]Error:[/red] {exc}", highlight=False)
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# nova kg ingest
# ---------------------------------------------------------------------------


@kg_app.command("ingest")
def kg_ingest_cmd(
    capsule_dir: Annotated[
        Optional[Path],
        typer.Argument(
            help=(
                "Path to a capsule directory (local-dir source mode).  "
                "Omit when using --source nats or --all."
            ),
        ),
    ] = None,
    path: _KgPathArg = _DEFAULT_KG_PATH,
    novaseal_verified: Annotated[
        bool,
        typer.Option(
            "--verified/--no-verified",
            help="Mark all events as NovaSeal-verified (sets confidence=1.0).",
        ),
    ] = False,
    all_capsules: Annotated[
        bool,
        typer.Option(
            "--all",
            help=(
                "Ingest ALL capsule directories under --capsule-dir "
                "(default: $NOVAFABRIC_CAPSULE_DIR or $NOVAFABRIC_HOME/capsules). "
                "Mutually exclusive with CAPSULE_DIR positional argument."
            ),
        ),
    ] = False,
    capsule_dir_base: Annotated[
        Optional[Path],
        typer.Option(
            "--capsule-dir",
            help=(
                "Base directory to scan when using --all. "
                "Defaults to $NOVAFABRIC_CAPSULE_DIR or $NOVAFABRIC_HOME/capsules."
            ),
        ),
    ] = None,
    source: Annotated[
        str,
        typer.Option(
            "--source",
            help=(
                "Event source type: 'local-dir' (default) or 'nats'. "
                "When 'nats', supply --nats-url and --nats-subject."
            ),
        ),
    ] = "local-dir",
    nats_url: Annotated[
        str,
        typer.Option(
            "--nats-url",
            help="NATS server URL (used with --source nats).",
        ),
    ] = "nats://localhost:4222",
    nats_subject: Annotated[
        str,
        typer.Option(
            "--nats-subject",
            help="NATS JetStream subject to consume (used with --source nats).",
        ),
    ] = "nova.kg.events",
) -> None:
    """Ingest capsule events into the Knowledge Graph.

    Three modes:
      Local-dir   nova kg ingest path/to/capsule/
      Bulk        nova kg ingest --all
      NATS stream nova kg ingest --source nats --nats-url nats://localhost:4222

    Scope: one or more capsules (or NATS stream).

    Model/tool-call granularity via --source nats (2026-07-30, ADR-0220
    follow-up): capture/orchestrator.py's real producer now re-emits each
    locally-captured model/tool call as a ModelCallCompleted/ModelCallFailed/
    ToolCallCompleted/ToolCallFailed spool event once the run finishes
    (no *Started variant — the source data has one record per completed/
    failed call, never a separate start event), so this ingestion path
    produces real CALLS/USES_TOOL edges from real NATS traffic. EndpointRouted
    is still never emitted by any producer. --source local-dir and --all read
    the same underlying model-calls.jsonl/tool-calls.jsonl files directly and
    were unaffected either way.

    \b
    Examples:
      # Ingest a single capsule
      nova kg ingest path/to/my-capsule/

      # Ingest all capsules in the default capsule directory
      nova kg ingest --all

      # Ingest from a NATS JetStream subject
      nova kg ingest --source nats --nats-url nats://localhost:4222
    """
    # ── --all mode ──────────────────────────────────────────────────────────
    if all_capsules:
        from rich.progress import Progress, SpinnerColumn, TextColumn  # noqa: PLC0415

        from novafabric._paths import default_capsule_dir  # noqa: PLC0415

        base = capsule_dir_base or default_capsule_dir()
        if not base.is_dir():
            console.print(
                f"[red]Error:[/red] Capsule base directory not found: {base}",
                highlight=False,
            )
            raise typer.Exit(1)

        try:
            from novafabric.kg.pipeline import KGIngestionPipeline  # noqa: PLC0415
            from novafabric.kg.store import KGStore  # noqa: PLC0415
        except ImportError as exc:
            console.print(f"[red]Error:[/red] {exc}", highlight=False)
            raise typer.Exit(1)

        store = KGStore(path)
        store.init_schema()
        pipeline = KGIngestionPipeline(store)
        # KG ingest reads model-calls.jsonl / tool-calls.jsonl / events.jsonl
        # directly and never reads capsule.yaml, so discover capsule dirs by the
        # presence of an ingestable event file rather than requiring a manifest
        # (which would skip event-only capture dirs). Real capsules always have
        # capsule.yaml too, so this is a strict superset of the manifest scan.
        from novafabric.serve.capsule_loader import (  # noqa: PLC0415
            discover_ingestable_dirs,
        )
        dirs = discover_ingestable_dirs(base)

        total_ingested = 0
        total_skipped = 0
        total_written = 0
        failed = 0

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
            transient=True,
        ) as progress:
            ptask = progress.add_task(f"Ingesting {len(dirs)} capsule(s)…", total=len(dirs))
            for i, cdir in enumerate(dirs, 1):
                progress.update(ptask, description=f"[{i}/{len(dirs)}] {cdir.name}")
                try:
                    ev_files: list[Path] = []
                    for fname in ("model-calls.jsonl", "tool-calls.jsonl"):
                        p = cdir / fname
                        if p.exists():
                            ev_files.append(p)
                    if not ev_files:
                        fallback = cdir / "events.jsonl"
                        if fallback.exists():
                            ev_files.append(fallback)
                    cap_ingested = 0
                    cap_skipped = 0
                    for evf in ev_files:
                        for raw in evf.read_text(encoding="utf-8").splitlines():
                            raw = raw.strip()
                            if not raw:
                                continue
                            try:
                                event = json.loads(raw)
                                pipeline.ingest_event(event, novaseal_valid=novaseal_verified)
                                cap_ingested += 1
                            except json.JSONDecodeError:
                                cap_skipped += 1
                    written = pipeline.flush_to_store()
                    total_ingested += cap_ingested
                    total_skipped += cap_skipped
                    total_written += written
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Failed to ingest %s: %s", cdir.name, exc)
                    failed += 1
                progress.advance(ptask)

        console.print(
            f"Bulk ingest complete: [cyan]{len(dirs)}[/cyan] capsule(s) scanned · "
            f"[green]{total_ingested}[/green] events ingested · "
            f"[green]{total_written}[/green] KG edges written · "
            f"[yellow]{total_skipped}[/yellow] skipped · "
            f"[red]{failed}[/red] failed"
        )
        return

    # Validate early: for local-dir mode, CAPSULE_DIR must be provided.
    if source == "local-dir" and capsule_dir is None:
        console.print(
            "[red]Error:[/red] CAPSULE_DIR is required for --source local-dir "
            "(or use --all to ingest all capsule directories).",
            highlight=False,
        )
        raise typer.Exit(1)

    import asyncio
    from typing import Any as _Any
    from typing import Coroutine as _Coroutine

    def _run_async(coro: _Coroutine[_Any, _Any, int]) -> int:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    try:
        from novafabric.kg.pipeline import KGIngestionPipeline
        from novafabric.kg.store import KGStore
    except ImportError as exc:
        console.print(f"[red]Error:[/red] {exc}", highlight=False)
        raise typer.Exit(1)

    store = KGStore(path)
    store.init_schema()
    pipeline = KGIngestionPipeline(store)

    if source == "nats":
        try:
            from novafabric.kg.consumer import CapsuleEventConsumer  # noqa: PLC0415
        except ImportError as exc:
            console.print(f"[red]Error:[/red] {exc}", highlight=False)
            raise typer.Exit(1)

        async def _run_nats() -> int:
            consumer = CapsuleEventConsumer()
            count = 0
            async for event in consumer.iter_from_nats(nats_url, nats_subject):
                pipeline.ingest_event(event, novaseal_valid=novaseal_verified)
                count += 1
            return count

        ingested = _run_async(_run_nats())
        written = pipeline.flush_to_store()
        console.print(
            f"Ingested [cyan]{ingested}[/cyan] NATS events → "
            f"wrote [green]{written}[/green] KG edges to {path}"
        )
        return

    # Local-dir mode (default)
    # capsule_dir is guaranteed non-None here (checked above before kuzu import)
    assert capsule_dir is not None  # noqa: S101

    if not capsule_dir.is_dir():
        console.print(
            f"[red]Error:[/red] {capsule_dir} is not a directory.",
            highlight=False,
        )
        raise typer.Exit(1)

    # Collect all event files: model-calls.jsonl + tool-calls.jsonl (preferred),
    # fall back to events.jsonl when neither exists.
    event_files: list[Path] = []
    for candidate in ("model-calls.jsonl", "tool-calls.jsonl"):
        p = capsule_dir / candidate
        if p.exists():
            event_files.append(p)
    if not event_files:
        fallback = capsule_dir / "events.jsonl"
        if fallback.exists():
            event_files.append(fallback)

    if not event_files:
        console.print(
            f"[red]Error:[/red] No events file found in {capsule_dir} "
            "(expected model-calls.jsonl, tool-calls.jsonl, or events.jsonl).",
            highlight=False,
        )
        raise typer.Exit(1)

    ingested = 0
    skipped = 0
    for events_file in event_files:
        for raw_line in events_file.read_text(encoding="utf-8").splitlines():
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                event = json.loads(raw_line)
                pipeline.ingest_event(event, novaseal_valid=novaseal_verified)
                ingested += 1
            except json.JSONDecodeError as exc:
                logger.debug("Skipping invalid JSON line: %s", exc)
                skipped += 1

    written = pipeline.flush_to_store()
    console.print(
        f"Ingested [cyan]{ingested}[/cyan] events "
        f"([yellow]{skipped}[/yellow] skipped) → "
        f"wrote [green]{written}[/green] KG edges to {path}"
    )


# ---------------------------------------------------------------------------
# nova kg query
# ---------------------------------------------------------------------------


@kg_app.command("query")
def kg_query_cmd(
    agent_id: Annotated[
        str,
        typer.Argument(help="Agent ID to query."),
    ],
    path: _KgPathArg = _DEFAULT_KG_PATH,
    output: Annotated[
        str,
        typer.Option("--output", "-o", help="Output format: json|text"),
    ] = "json",
) -> None:
    """Query models and tools called by an agent.

    Scope: single agent (reads from the KG store).

    \b
    Examples:
      nova kg query my-agent-id
      nova kg query my-agent-id --output text
    """
    try:
        from novafabric.kg.store import KGStore
    except ImportError as exc:
        console.print(f"[red]Error:[/red] {exc}", highlight=False)
        raise typer.Exit(1)

    try:
        store = KGStore(path)
        models = store.query_agent_models(agent_id)
        tools = store.query_agent_tools(agent_id)
        mcp_servers = store.query_agent_mcp_servers(agent_id)
        result = {
            "agent_id": agent_id,
            "models": models,
            "tools": tools,
            "mcp_servers": mcp_servers,
        }
        if output == "json":
            typer.echo(json.dumps(result, indent=2))
        else:
            if models:
                console.print(f"\n[bold]Models called by {agent_id}:[/bold]")
                for row in models:
                    console.print(
                        f"  {row['model_id']}  calls={row['call_count']}  "
                        f"confidence={row['confidence']:.2f}"
                    )
            if tools:
                console.print(f"\n[bold]Tools used by {agent_id}:[/bold]")
                for row in tools:
                    console.print(
                        f"  {row['tool_id']}  calls={row['call_count']}  "
                        f"confidence={row['confidence']:.2f}"
                    )
            if mcp_servers:
                console.print(f"\n[bold]MCP servers reachable from {agent_id}:[/bold]")
                for row in mcp_servers:
                    console.print(
                        f"  {row['server_id']}  calls={row['call_count']}"
                    )
            if not models and not tools and not mcp_servers:
                console.print(f"No KG entries found for agent {agent_id!r}")
    except Exception as exc:
        console.print(f"[red]Error:[/red] {exc}", highlight=False)
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# nova kg audit
# ---------------------------------------------------------------------------


@kg_app.command("audit")
def kg_audit_cmd(
    path: _KgPathArg = _DEFAULT_KG_PATH,
    output: Annotated[
        str,
        typer.Option("--output", "-o", help="Output format: json|text"),
    ] = "text",
) -> None:
    """Check KG health: node/edge counts, orphaned edges, zero-call-count anomalies.

    Exit code 0 = no issues found.
    Exit code 1 = issues detected (see output for details).

    Scope: knowledge graph (reads the full KG store).

    \b
    Examples:
      nova kg audit
      nova kg audit --output json
    """
    try:
        from novafabric.kg.store import KGStore
    except ImportError as exc:
        console.print(f"[red]Error:[/red] {exc}", highlight=False)
        raise typer.Exit(1)

    try:
        store = KGStore(path)
        audit = store.get_audit()
    except Exception as exc:
        console.print(f"[red]Error:[/red] {exc}", highlight=False)
        raise typer.Exit(1)

    if output == "json":
        typer.echo(json.dumps(audit, indent=2))
    else:
        health = audit.get("store_health", "unknown")
        color = "green" if health == "ok" else "red"
        console.print(f"[{color}]KG store health:[/{color}] {health}")
        console.print(f"  db_path: {audit.get('db_path')}")
        for key, val in audit.items():
            if key.startswith("node_") or key.startswith("edge_"):
                console.print(f"  {key}: {val}")
        issues = audit.get("issues", [])
        if issues:
            console.print("\n[yellow]Issues detected:[/yellow]")
            for issue in issues:
                console.print(f"  [yellow]•[/yellow] {issue}")
        else:
            console.print("\n[green]No issues detected.[/green]")

    if audit.get("issues"):
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# nova kg alias list <canonical>
# ---------------------------------------------------------------------------


@alias_app.command("list")
def kg_alias_list_cmd(
    canonical: Annotated[
        str,
        typer.Argument(help="Canonical entity name to list aliases for."),
    ],
    alias_db: _AliasDBAr = _DEFAULT_ALIAS_DB_PATH,
    output: Annotated[
        str,
        typer.Option("--output", "-o", help="Output format: json|text"),
    ] = "text",
) -> None:
    """List all aliases registered for a canonical entity name.

    Scope: knowledge graph alias table.

    \b
    Examples:
      nova kg alias list "gpt-4o-mini-2024-07-18"
      nova kg alias list "my-agent" --output json
    """
    from novafabric.kg.alias_resolver import AliasTableResolver

    resolver = AliasTableResolver(db_path=Path(alias_db))
    entries = resolver.list_aliases(canonical)
    resolver.close()

    if output == "json":
        typer.echo(
            json.dumps(
                [e.model_dump(mode="json") for e in entries], indent=2, default=str
            )
        )
        return

    if not entries:
        console.print(
            f"[dim]No aliases registered for canonical name {canonical!r}.[/dim]"
        )
        return

    table = Table(title=f"Aliases for {canonical!r}")
    table.add_column("Alias")
    table.add_column("Entity Type")
    table.add_column("Confidence", justify="right")
    table.add_column("Source")
    table.add_column("Created At")
    for e in entries:
        table.add_row(
            e.alias,
            e.entity_type,
            f"{e.confidence:.3f}",
            e.source,
            e.created_at.isoformat(),
        )
    console.print(table)


# ---------------------------------------------------------------------------
# nova kg alias register <alias> <canonical> --type model|agent|tool
# ---------------------------------------------------------------------------


@alias_app.command("register")
def kg_alias_register_cmd(
    alias: Annotated[
        str,
        typer.Argument(help="Alias (non-canonical form seen in capsules)."),
    ],
    canonical: Annotated[
        str,
        typer.Argument(help="Canonical entity name to map to."),
    ],
    entity_type: Annotated[
        str,
        typer.Option("--type", "-t", help="Entity type: model|agent|tool|endpoint|mcp_server"),
    ] = "model",
    confidence: Annotated[
        float,
        typer.Option("--confidence", "-c", help="Confidence score (0.0–1.0)."),
    ] = 1.0,
    alias_db: _AliasDBAr = _DEFAULT_ALIAS_DB_PATH,
) -> None:
    """Register an alias → canonical entity mapping.

    Scope: knowledge graph alias table.

    \b
    Examples:
      nova kg alias register "gpt-4o-mini" "gpt-4o-mini-2024-07-18" --type model
      nova kg alias register "my-agent" "my-company/my-agent-v2" --type agent
    """
    from datetime import datetime, timezone

    from novafabric.kg.alias_resolver import AliasEntry, AliasTableResolver

    resolver = AliasTableResolver(db_path=Path(alias_db))
    entry = AliasEntry(
        alias=alias,
        canonical=canonical,
        entity_type=entity_type,
        confidence=confidence,
        source="manual",
        created_at=datetime.now(timezone.utc),
    )
    resolver.register(entry)
    resolver.close()
    console.print(
        f"[green]Registered:[/green] {alias!r} → {canonical!r} "
        f"(type={entity_type}, confidence={confidence:.3f})"
    )


# ---------------------------------------------------------------------------
# nova kg entity-queue list
# ---------------------------------------------------------------------------


@queue_app.command("list")
def kg_queue_list_cmd(
    queue_db: _QueueDBAr = _DEFAULT_QUEUE_DB_PATH,
    output: Annotated[
        str,
        typer.Option("--output", "-o", help="Output format: json|text"),
    ] = "text",
) -> None:
    """List all pending items in the Tier-3 entity review queue.

    Scope: knowledge graph review queue.

    \b
    Examples:
      nova kg entity-queue list
      nova kg entity-queue list --output json
    """
    from novafabric.kg.review_queue import HumanReviewQueueWriter

    q = HumanReviewQueueWriter(db_path=Path(queue_db))
    items = q.list_pending()
    q.close()

    if output == "json":
        typer.echo(
            json.dumps(
                [i.model_dump(mode="json") for i in items], indent=2, default=str
            )
        )
        return

    if not items:
        console.print("[dim]No pending entities in the review queue.[/dim]")
        return

    table = Table(title=f"Entity Review Queue ({len(items)} pending)")
    table.add_column("Item ID", style="dim", no_wrap=True)
    table.add_column("Alias")
    table.add_column("Type")
    table.add_column("Suggested Canonical")
    table.add_column("Confidence", justify="right")
    table.add_column("Created At")
    for item in items:
        table.add_row(
            item.item_id,
            item.alias,
            item.entity_type,
            item.suggested_canonical or "—",
            f"{item.confidence:.3f}",
            item.created_at.isoformat(),
        )
    console.print(table)


# ---------------------------------------------------------------------------
# nova kg entity-queue approve <item_id> --canonical <name>
# ---------------------------------------------------------------------------


@queue_app.command("approve")
def kg_queue_approve_cmd(
    item_id: Annotated[
        str,
        typer.Argument(help="UUID of the review item to approve."),
    ],
    canonical: Annotated[
        str,
        typer.Option("--canonical", "-c", help="Canonical name to assign."),
    ],
    resolved_by: Annotated[
        str,
        typer.Option("--by", help="Reviewer identifier (name or user ID)."),
    ] = "cli",
    queue_db: _QueueDBAr = _DEFAULT_QUEUE_DB_PATH,
) -> None:
    """Approve a review item and assign its canonical entity name.

    Scope: knowledge graph review queue.

    \b
    Examples:
      nova kg entity-queue approve <item-id> --canonical "gpt-4o-mini-2024-07-18"
      nova kg entity-queue approve <item-id> --canonical "my-agent" --by alice
    """
    from novafabric.kg.review_queue import HumanReviewQueueWriter

    q = HumanReviewQueueWriter(db_path=Path(queue_db))
    q.approve(item_id, canonical=canonical, resolved_by=resolved_by)
    q.close()
    console.print(
        f"[green]Approved:[/green] {item_id} → canonical={canonical!r} "
        f"(resolved_by={resolved_by!r})"
    )


# ---------------------------------------------------------------------------
# nova kg entity-queue reject <item_id>
# ---------------------------------------------------------------------------


@queue_app.command("reject")
def kg_queue_reject_cmd(
    item_id: Annotated[
        str,
        typer.Argument(help="UUID of the review item to reject."),
    ],
    resolved_by: Annotated[
        str,
        typer.Option("--by", help="Reviewer identifier (name or user ID)."),
    ] = "cli",
    queue_db: _QueueDBAr = _DEFAULT_QUEUE_DB_PATH,
) -> None:
    """Reject a review item without assigning a canonical name.

    Scope: knowledge graph review queue.

    \b
    Examples:
      nova kg entity-queue reject <item-id>
      nova kg entity-queue reject <item-id> --by alice
    """
    from novafabric.kg.review_queue import HumanReviewQueueWriter

    q = HumanReviewQueueWriter(db_path=Path(queue_db))
    q.reject(item_id, resolved_by=resolved_by)
    q.close()
    console.print(
        f"[yellow]Rejected:[/yellow] {item_id} (resolved_by={resolved_by!r})"
    )


# ---------------------------------------------------------------------------
# nova kg entity-queue stats
# ---------------------------------------------------------------------------


@queue_app.command("stats")
def kg_queue_stats_cmd(
    queue_db: _QueueDBAr = _DEFAULT_QUEUE_DB_PATH,
    output: Annotated[
        str,
        typer.Option("--output", "-o", help="Output format: json|text"),
    ] = "text",
) -> None:
    """Show pending/approved/rejected counts for the entity review queue.

    Scope: knowledge graph review queue.

    \b
    Examples:
      nova kg entity-queue stats
      nova kg entity-queue stats --output json
    """
    from novafabric.kg.review_queue import HumanReviewQueueWriter

    q = HumanReviewQueueWriter(db_path=Path(queue_db))
    s = q.stats()
    q.close()

    if output == "json":
        typer.echo(json.dumps(s, indent=2))
        return

    console.print(
        f"[bold]Entity Review Queue Stats[/bold]\n"
        f"  pending:  [cyan]{s['pending']}[/cyan]\n"
        f"  approved: [green]{s['approved']}[/green]\n"
        f"  rejected: [red]{s['rejected']}[/red]"
    )


# ---------------------------------------------------------------------------
# nova kg build-provenance (SPKG P1, ADR-0111 — experimental)
# ---------------------------------------------------------------------------


@kg_app.command("build-provenance")
def kg_build_provenance_cmd(
    capsule_dir: Annotated[
        Path,
        typer.Argument(help="Path to a capsule directory (reads its lineage.jsonl)."),
    ],
    output: Annotated[
        Optional[Path],
        typer.Option("--output", "-o", help="Write the RDF graph here (default: stdout)."),
    ] = None,
    rdf_format: Annotated[
        str,
        typer.Option("--format", help="RDF serialization: turtle | nt | json-ld."),
    ] = "turtle",
    validate: Annotated[
        bool,
        typer.Option(
            "--validate/--no-validate",
            help="SHACL-validate the graph (ADR-0111 R11); exit 1 if invalid facts are found.",
        ),
    ] = True,
) -> None:
    """Build the SPKG PROV-O provenance graph for a capsule (experimental, ADR-0111).

    Maps the capsule's ``lineage.jsonl`` to W3C PROV-O RDF and, by default, SHACL-validates
    it (R11: invalid facts are rejected). Requires ``novafabric[spkg]`` (rdflib + pyshacl).

    Scope: single capsule.

    \b
    Examples:
      nova kg build-provenance path/to/capsule/
      nova kg build-provenance path/to/capsule/ -o prov.ttl --format turtle
      nova kg build-provenance path/to/capsule/ --no-validate
    """
    try:
        from novafabric.kg.spkg.provo_mapping import (
            capsule_lineage_to_provo,
            validate_provo,
        )
    except ImportError as exc:
        console.print(
            f"[red]Error:[/red] {exc} — install the SPKG extra: pip install novafabric[spkg]",
            highlight=False,
        )
        raise typer.Exit(1)

    graph = capsule_lineage_to_provo(capsule_dir)
    triple_count = len(graph)

    if validate:
        conforms, report = validate_provo(graph)
        if not conforms:
            console.print(
                f"[red]✗[/red] SHACL validation failed for {capsule_dir.name} "
                f"({triple_count} triples):",
                highlight=False,
            )
            console.print(report, highlight=False)
            raise typer.Exit(1)
        console.print(
            f"[green]✓[/green] SHACL-valid: {triple_count} PROV-O triples "
            f"from {capsule_dir.name}"
        )

    serialized = graph.serialize(format=rdf_format)
    if output is not None:
        output.write_text(serialized, encoding="utf-8")
        console.print(f"[green]✓[/green] provenance graph written: {output}")
    else:
        typer.echo(serialized)


# ---------------------------------------------------------------------------
# nova kg build (SPKG P1, ADR-0111 — experimental)
# ---------------------------------------------------------------------------

_DEFAULT_SPKG_PATH = ".nova/kg/spkg.kuzu"


@kg_app.command("build")
def kg_build_cmd(
    capsule_dir: Annotated[
        Path,
        typer.Argument(help="Path to a capsule directory (reads its lineage.jsonl)."),
    ],
    path: Annotated[
        str,
        typer.Option(
            "--path",
            envvar="NOVA_SPKG_PATH",
            help="KùzuDB path for the SPKG operational graph (separate from the Capsule KG).",
        ),
    ] = _DEFAULT_SPKG_PATH,
    validate: Annotated[
        bool,
        typer.Option(
            "--validate/--no-validate",
            help="SHACL-gate the canonical layer before writing the LPG (ADR-0111 R11).",
        ),
    ] = True,
) -> None:
    """Build the SPKG for a capsule: canonical PROV-O (SHACL-gated) + operational LPG.

    The canonical RDF layer is validated first (R11) — on failure nothing is written to the
    operational KùzuDB store; then the LPG is (re)built from the same capsule (R4: the LPG
    holds no state not derivable from the capsule). Requires ``novafabric[spkg]``.

    Scope: single capsule.

    \b
    Examples:
      nova kg build path/to/capsule/
      nova kg build path/to/capsule/ --path .nova/kg/spkg.kuzu
      nova kg build path/to/capsule/ --no-validate
    """
    try:
        from novafabric.kg.spkg.build import SpkgValidationError, build_spkg
        from novafabric.kg.spkg.graph_store import SpkgGraphStore
    except ImportError as exc:
        console.print(
            f"[red]Error:[/red] {exc} — install the SPKG extra: pip install novafabric[spkg]",
            highlight=False,
        )
        raise typer.Exit(1)

    store = SpkgGraphStore(path)
    try:
        result = build_spkg(capsule_dir, store, validate=validate)
    except SpkgValidationError as exc:
        console.print(f"[red]✗[/red] {exc}", highlight=False)
        raise typer.Exit(1)
    finally:
        store.close()

    gate = "SHACL-valid" if result.validated else "unvalidated"
    console.print(
        f"[green]✓[/green] SPKG built from {capsule_dir.name} ({gate}): "
        f"{result.triples} PROV-O triples · {result.edges_ingested} LPG edges → {path}"
    )


# ---------------------------------------------------------------------------
# nova kg detect (SPKG P1, ADR-0111 SP-2 baseline — experimental)
# ---------------------------------------------------------------------------


@kg_app.command("detect")
def kg_detect_cmd(
    capsule_dir: Annotated[
        Path,
        typer.Argument(help="Capsule directory to score for anomalous lineage edges."),
    ],
    baseline: Annotated[
        list[Path],
        typer.Option(
            "--baseline",
            help="Capsule dir(s) to learn 'normal' from (repeatable). "
            "Default: self-baseline on the target capsule.",
        ),
    ] = [],
    top: Annotated[
        int, typer.Option("--top", "-k", help="Number of most-anomalous edges to report.")
    ] = 5,
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Emit schema-valid AnomalyFinding records instead of a table."),
    ] = False,
    output: Annotated[
        Optional[Path],
        typer.Option("--output", "-o", help="Write findings JSON here (with --json)."),
    ] = None,
) -> None:
    """Rank the most anomalous lineage edges in a capsule (unsupervised, no labels).

    A Tier-A structural outlier detector learns the fleet's own edge distribution (from
    ``--baseline`` capsules, or the target itself) and flags edges with high combined
    surprisal. Every reported edge carries a MITRE ATT&CK technique (ADR-0111 R2 — no bare
    score). This is the dependency-free SP-2 baseline; the PyGOD/TGN GNN detector is a later,
    resource-gated upgrade. **Needs no optional extra.**

    Scope: single capsule (optionally baselined on a corpus).

    \b
    Examples:
      nova kg detect path/to/capsule/
      nova kg detect path/to/capsule/ --baseline normal1/ --baseline normal2/ -k 10
      nova kg detect path/to/capsule/ --json -o findings.json
    """
    from novafabric.kg.spkg.detect import StructuralAnomalyDetector, to_findings
    from novafabric.kg.spkg.provo_mapping import read_lineage_edges

    target_edges = read_lineage_edges(capsule_dir)
    if not target_edges:
        console.print(f"[yellow]No lineage edges in {capsule_dir.name}.[/yellow]")
        return

    fit_edges: list[dict[str, Any]] = []
    for b in baseline:
        fit_edges.extend(read_lineage_edges(b))
    if not fit_edges:
        fit_edges = target_edges  # self-baseline

    detector = StructuralAnomalyDetector().fit(fit_edges)
    scored = detector.top_k(target_edges, k=top)

    if as_json:
        findings = to_findings(scored)
        text = json.dumps(findings, indent=2)
        if output is not None:
            output.write_text(text + "\n", encoding="utf-8")
            console.print(f"[green]✓[/green] {len(findings)} findings written: {output}")
        else:
            typer.echo(text)
        return

    baseline_note = (
        f"{len(fit_edges)} baseline edges" if baseline else "self-baseline"
    )
    table = Table(
        title=f"SPKG anomaly scan — {capsule_dir.name} (top {len(scored)}, {baseline_note})"
    )
    table.add_column("#", justify="right")
    table.add_column("score", justify="right")
    table.add_column("edge_type")
    table.add_column("source → target")
    table.add_column("ATT&CK")
    for i, se in enumerate(scored, 1):
        src = se.edge.get("source", {}) or {}
        tgt = se.edge.get("target", {}) or {}
        edge_flow = f"{src.get('ref', '?')} → {tgt.get('ref', '?')}"
        technique = to_findings([se])[0]["explanation"]["attack_technique_id"]
        table.add_row(
            str(i),
            f"{se.score:.3f}",
            str(se.edge.get("edge_type", "")),
            edge_flow,
            technique,
        )
    console.print(table)


# ---------------------------------------------------------------------------
# nova kg attack-path / blast-radius (SPKG UC2/UC3, ADR-0111 — experimental)
# ---------------------------------------------------------------------------


def _parse_entity(spec: str) -> tuple[str, str]:
    """Parse a ``kind:ref`` entity spec (ref may itself contain ':')."""
    if ":" not in spec:
        console.print(
            f"[red]Error:[/red] entity must be 'kind:ref' (got {spec!r}), "
            "e.g. run:my-run or model:gpt-4o",
            highlight=False,
        )
        raise typer.Exit(2)
    kind, ref = spec.split(":", 1)
    return kind, ref


def _spkg_store_from_capsule(capsule_dir: Path) -> SpkgGraphStore:
    """Build a throwaway in-process SPKG LPG from a capsule's lineage edges."""
    try:
        from novafabric.kg.spkg.graph_store import SpkgGraphStore
        from novafabric.kg.spkg.provo_mapping import read_lineage_edges
    except ImportError as exc:
        console.print(
            f"[red]Error:[/red] {exc} — install the SPKG extra: pip install novafabric[spkg]",
            highlight=False,
        )
        raise typer.Exit(1)
    edges = read_lineage_edges(capsule_dir)
    store = SpkgGraphStore()
    store.ingest_edges(edges)
    return store


@kg_app.command("attack-path")
def kg_attack_path_cmd(
    capsule_dir: Annotated[
        Path, typer.Argument(help="Capsule directory (its lineage builds the SPKG graph).")
    ],
    from_entity: Annotated[
        str, typer.Option("--from", help="Source entity as 'kind:ref', e.g. run:attacker.")
    ],
    to_entity: Annotated[
        str, typer.Option("--to", help="Target entity as 'kind:ref', e.g. dataset:secrets.")
    ],
    max_depth: Annotated[
        int, typer.Option("--max-depth", help="Maximum path length to search.")
    ] = 6,
) -> None:
    """Shortest attack path between two entities in the SPKG (UC2 lateral-movement).

    Builds the operational graph from the capsule's lineage, then runs a bounded
    shortest-path query. Exit 0 whether or not a path exists (informational).

    \b
    Examples:
      nova kg attack-path capsule/ --from run:attacker --to dataset:aws_credentials
      nova kg attack-path capsule/ --from agent:a1 --to artifact:report.md --max-depth 4
    """
    from novafabric.lineage._types import node_id_for

    s_kind, s_ref = _parse_entity(from_entity)
    t_kind, t_ref = _parse_entity(to_entity)
    store = _spkg_store_from_capsule(capsule_dir)
    try:
        hops = store.attack_path(
            node_id_for(s_kind, s_ref), node_id_for(t_kind, t_ref), max_depth=max_depth
        )
    finally:
        store.close()

    if hops is None:
        console.print(
            f"[green]✓[/green] No attack path {from_entity} → {to_entity} "
            f"within {max_depth} hops.",
            highlight=False,
        )
    else:
        console.print(
            f"[yellow]⚠ Attack path found:[/yellow] {from_entity} → {to_entity} "
            f"in [bold]{hops}[/bold] hop(s).",
            highlight=False,
        )


@kg_app.command("blast-radius")
def kg_blast_radius_cmd(
    capsule_dir: Annotated[
        Path, typer.Argument(help="Capsule directory (its lineage builds the SPKG graph).")
    ],
    entity: Annotated[
        str, typer.Option("--entity", help="Entity as 'kind:ref', e.g. model:gpt-4o.")
    ],
    upstream: Annotated[
        bool,
        typer.Option(
            "--upstream/--downstream",
            help="--downstream (default): what this entity affects (blast radius, UC3). "
            "--upstream: what influenced it (provenance).",
        ),
    ] = False,
    max_depth: Annotated[
        int, typer.Option("--max-depth", help="Maximum traversal depth.")
    ] = 6,
) -> None:
    """Impact/blast-radius of an entity across the SPKG (UC3 supply-chain propagation).

    ``--downstream`` (default) lists everything reachable *from* the entity — e.g. the runs
    and artifacts a poisoned model touched. ``--upstream`` lists its provenance.

    \b
    Examples:
      nova kg blast-radius capsule/ --entity model:poisoned-model
      nova kg blast-radius capsule/ --entity artifact:report.md --upstream
    """
    from novafabric.lineage._types import node_id_for

    kind, ref = _parse_entity(entity)
    store = _spkg_store_from_capsule(capsule_dir)
    try:
        node_id = node_id_for(kind, ref)
        affected = (
            store.ancestors(node_id, max_depth)
            if upstream
            else store.descendants(node_id, max_depth)
        )
    finally:
        store.close()

    direction = "provenance (upstream)" if upstream else "blast radius (downstream)"
    if not affected:
        console.print(
            f"[green]✓[/green] No {direction} for {entity} within {max_depth} hops.",
            highlight=False,
        )
        return
    table = Table(
        title=f"SPKG {direction} — {entity} (≤{max_depth} hops, {len(affected)} entities)"
    )
    table.add_column("kind")
    table.add_column("ref")
    for _nid, k, r in sorted(affected, key=lambda x: (x[1], x[2])):
        table.add_row(k, r)
    console.print(table)
