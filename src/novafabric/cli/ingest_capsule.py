"""`nova ingest-capsule` — populate runs_cache from one or all capsules.

Modes:
  nova ingest-capsule <run_id>   — single targeted ingest (by run_id or path)
  nova ingest-capsule --all      — re-index every capsule in capsule-dir
  nova ingest-capsule --watch    — foreground watcher loop (Ctrl+C to stop)
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console

from novafabric._paths import default_capsule_dir, registry_db_path

console = Console()
app = typer.Typer(add_completion=False, invoke_without_command=True)


@app.command()
def ingest_capsule_cmd(
    run_id: Annotated[
        Optional[str],
        typer.Argument(help="Run ID to ingest (required unless --all or --watch)"),
    ] = None,
    all_capsules: Annotated[
        bool,
        typer.Option("--all", help="Re-index all capsules in capsule-dir"),
    ] = False,
    watch: Annotated[
        bool,
        typer.Option("--watch", help="Run foreground watcher loop (Ctrl+C to stop)"),
    ] = False,
    interval: Annotated[
        float,
        typer.Option("--interval", help="Watcher poll interval in seconds (--watch only)"),
    ] = 2.0,
    backend: Annotated[
        str,
        typer.Option("--backend", help="Backend: auto, polling, watchdog"),
    ] = "auto",
    capsule_dir: Annotated[
        Optional[Path],
        typer.Option("--capsule-dir", help="Override NOVAFABRIC_CAPSULE_DIR"),
    ] = None,
    db_path: Annotated[
        Optional[Path],
        typer.Option("--db-path", help="Override NOVAFABRIC_DB_PATH"),
    ] = None,
) -> None:
    """Index capsule files into the runs metadata store.

    Three modes:
      Single run   nova ingest-capsule <run-id-or-path>
      All capsules nova ingest-capsule --all
      Watch mode   nova ingest-capsule --watch  (foreground, Ctrl+C to stop)

    Scope: one capsule, all capsules, or continuous (watch mode).

    \b
    Examples:
      # Ingest a single capsule by run ID
      nova ingest-capsule 01HX...

      # Re-index all capsules in the default capsule directory
      nova ingest-capsule --all

      # Watch for new capsules and ingest them automatically
      nova ingest-capsule --watch --interval 5
    """
    from novafabric.serve.capsule_watcher import CapsuleWatcher  # noqa: PLC0415

    resolved_dir = (capsule_dir or default_capsule_dir()).resolve()
    resolved_db = db_path or registry_db_path()

    if not resolved_dir.exists():
        console.print(
            f"[yellow]warning:[/yellow] capsule dir does not exist: {resolved_dir}"
        )
        if not watch:
            raise typer.Exit(code=0)

    watcher = CapsuleWatcher(
        resolved_dir, db_path=resolved_db, interval=interval, backend=backend
    )

    # ── --watch mode ─────────────────────────────────────────────────────
    if watch:
        from datetime import datetime  # noqa: PLC0415

        from novafabric.registry.runs_cache import query_runs  # noqa: PLC0415
        from novafabric.registry.store import get_connection, init_schema  # noqa: PLC0415

        console.print(
            f"watching [bold]{resolved_dir}[/bold]  "
            f"[dim][backend: {watcher.backend_name()}, interval: {interval} s][/dim]"
        )
        console.print("[dim]Press Ctrl+C to stop.[/dim]\n")

        _conn = get_connection(resolved_db)
        init_schema(_conn)
        _prev_ids: set[str] = {
            r["run_id"] for r in query_runs(_conn, limit=100_000)[0]
            if r.get("run_id")
        }
        _conn.close()

        try:
            while True:
                n = watcher.poll_once()
                if n > 0:
                    _conn = get_connection(resolved_db)
                    init_schema(_conn)
                    _rows, _ = query_runs(_conn, limit=100_000)
                    _conn.close()
                    _ts = datetime.now().strftime("%H:%M:%S")
                    _new_ids = {r["run_id"] for r in _rows if r.get("run_id")} - _prev_ids
                    for _row in _rows:
                        if _row.get("run_id") in _new_ids:
                            console.print(
                                f"[dim]{_ts}[/dim]  indexed  "
                                f"run_id=[bold]{_row['run_id']}[/bold]  [new]"
                            )
                    _prev_ids |= _new_ids
                time.sleep(interval)
        except KeyboardInterrupt:
            pass
        console.print("\nstopped.")
        return

    # ── --all mode ────────────────────────────────────────────────────────
    if all_capsules:
        console.print(f"scanning [bold]{resolved_dir}[/bold] ...")
        n = watcher.ingest_all()
        console.print(f"indexed [bold]{n}[/bold] capsule(s)")
        return

    # ── single ingest ─────────────────────────────────────────────────────
    if run_id is None:
        console.print("[red]error:[/red] provide a RUN_ID, --all, or --watch")
        raise typer.Exit(code=1)

    found, is_new = watcher.ingest_one(run_id)
    if not found:
        console.print(
            f"[red]not found:[/red] no capsule for run_id={run_id!r} in {resolved_dir}"
        )
        raise typer.Exit(code=1)

    label = r"\[new]" if is_new else r"\[updated]"
    console.print(
        f"indexed  run_id=[bold]{run_id}[/bold]  "
        f"capsule={resolved_dir / run_id}  {label}"
    )
