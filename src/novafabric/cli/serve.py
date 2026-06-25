"""`nova serve --experimental` — Typer command for the local dashboard.

Per ADR-0027, this is opt-in (requires `pip install novafabric[serve]` for
fastapi + uvicorn) and gated behind --experimental (mandatory flag).
"""

from __future__ import annotations

import threading
import time
import webbrowser
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.panel import Panel

from novafabric._paths import default_capsule_dir

console = Console()

_EXPERIMENTAL_BANNER = """\
[bold yellow]nova serve is EXPERIMENTAL.[/bold yellow]

This dashboard:
  • is opt-in (you ran `--experimental` to enable it),
  • binds to 127.0.0.1 only (localhost),
  • requires a one-shot session token on /api/* routes,
  • is read-only (Layer A); mutations are not yet supported.

Schemas, endpoints, and behaviour may change. The CLI remains canonical —
every dashboard view surfaces the equivalent `nova` command.
"""


def serve_cmd(
    experimental: Annotated[
        bool,
        typer.Option(
            "--experimental",
            help="Required. Acknowledges this is an experimental subcommand.",
        ),
    ] = False,
    host: Annotated[
        str,
        typer.Option(
            "--host",
            help="Interface to bind to. Localhost only by default.",
        ),
    ] = "127.0.0.1",
    port: Annotated[
        int,
        typer.Option(
            "--port",
            help="TCP port for the HTTP server.",
        ),
    ] = 4321,
    capsule_dir: Annotated[
        Path | None,
        typer.Option(
            "--capsule-dir",
            help="Directory of capsules to browse. Defaults to $NOVAFABRIC_HOME/capsules.",
        ),
    ] = None,
    db_path: Annotated[
        Path | None,
        typer.Option(
            "--db-path",
            help="Registry/lineage SQLite path. Defaults to ~/.novafabric/registry.db.",
        ),
    ] = None,
    no_browser: Annotated[
        bool,
        typer.Option("--no-browser", help="Don't auto-open a browser."),
    ] = False,
    insecure: Annotated[
        bool,
        typer.Option(
            "--insecure",
            help="Allow non-localhost binds. Dangerous; not for daily use.",
            hidden=True,
        ),
    ] = False,
    topology: Annotated[
        bool,
        typer.Option(
            "--topology",
            help="Enable live topology dashboard (Louvain cluster view + TDP WebSocket).",
        ),
    ] = False,
    tv5: Annotated[
        bool,
        typer.Option(
            "--tv5",
            help="Enable experimental 3D topology view (TV-5). Implied by --topology.",
        ),
    ] = False,
    topology_louvain_resolution: Annotated[
        float | None,
        typer.Option(
            "--topology-louvain-resolution",
            help="Louvain clustering resolution for the topology view. Lower values "
            "merge into fewer/larger clusters; higher values split further. "
            "Default 1.0 (or NOVA_TOPOLOGY_LOUVAIN_RESOLUTION).",
        ),
    ] = None,
) -> None:
    """Start the read-only local dashboard.

    Binds to 127.0.0.1 only. Requires --experimental and the
    'novafabric[serve]' extra (`pip install novafabric[serve]`).

    Scope: run-time (long-running server process).

    \b
    Examples:
      # Start on the default port (4321)
      nova serve --experimental

      # Start with the 3D topology view enabled
      nova serve --experimental --topology

      # Bind to a custom port
      nova serve --experimental --port 8080

      # Open the browser automatically
      nova serve --experimental --open
    """
    # Gate: --experimental is mandatory until graduation per ADR-0027.
    if not experimental:
        console.print(Panel(
            _EXPERIMENTAL_BANNER + "\n\n"
            "[bold]To start the dashboard:[/bold] "
            "`nova serve --experimental`",
            title="nova serve",
            border_style="yellow",
        ))
        raise typer.Exit(code=0)

    # Bind safety — checked before any import so this security gate is always
    # reachable even when the [serve] extra is not installed.
    if host not in {"127.0.0.1", "localhost", "::1"} and not insecure:
        console.print(Panel(
            f"[bold red]Refusing to bind to {host} without --insecure.[/bold red]\n\n"
            "The dashboard authenticates with a token but is designed for "
            "single-machine use. If you really need a non-localhost bind, pass "
            "[bold]--insecure[/bold] and put it behind your own TLS terminator.",
            border_style="red",
        ))
        raise typer.Exit(code=2)

    # Lazy import: FastAPI/uvicorn live in the [serve] extra. If they aren't
    # installed, give a clear instruction rather than a stack trace.
    try:
        import uvicorn  # noqa: F401
        from fastapi import FastAPI  # noqa: F401
    except ImportError:
        console.print(Panel(
            "[bold red]The `serve` extra is not installed.[/bold red]\n\n"
            "Install it with:\n\n"
            "  [bold]pip install 'novafabric[serve]'[/bold]\n\n"
            "(adds fastapi + uvicorn — both Apache-2.0 / MIT, ADR-0024 Tier A).",
            title="nova serve",
            border_style="red",
        ))
        raise typer.Exit(code=1)

    # Resolve directories
    resolved_capsule_dir = (capsule_dir or default_capsule_dir()).resolve()
    if not resolved_capsule_dir.exists():
        # Not fatal — user might want to browse the registry alone.
        console.print(
            f"[yellow]warning:[/yellow] capsule directory does not exist: "
            f"{resolved_capsule_dir} — `/api/runs` will return an empty list."
        )

    # Token + auth
    from novafabric.serve.app import create_app
    from novafabric.serve.auth import generate_token, write_token_file

    token = generate_token()
    write_token_file(token)

    # Static dashboard (optional — ships in the wheel; absent during dev)
    static_dir = Path(__file__).parent.parent / "serve" / "static"
    static_arg = static_dir if static_dir.exists() else None

    # Build the app WITHOUT the static mount so that routers added below
    # (TV-5) are registered before the "/" catch-all StaticFiles mount.
    app = create_app(
        token=token,
        capsule_dir=resolved_capsule_dir,
        db_path=db_path,
        static_dir=None,          # mounted last — see below
        topology_enabled=topology,
        topology_louvain_resolution=topology_louvain_resolution,
    )

    if topology or tv5:
        import logging as _logging
        _tv5_logger = _logging.getLogger(__name__)
        try:
            from novafabric.serve.topology.layout_pipeline_3d import LayoutPipeline3D
            from novafabric.serve.topology.router_tv5 import make_tv5_router
            from novafabric.serve.topology.snapshot_store_3d import SnapshotStore3D

            snap_store = SnapshotStore3D()
            layout_pipe = LayoutPipeline3D(snap_store)
            tv5_router = make_tv5_router(snap_store, layout_pipe)
            app.include_router(tv5_router)
            app.state.tv5_layout_pipe = layout_pipe  # exposed to seed endpoint
            _tv5_logger.info("TV-5 3D topology router mounted at /api/tv5/")
        except Exception as _e:  # noqa: BLE001
            console.print(f"[yellow]warning:[/yellow] TV-5 router failed to mount: {_e}")

    # Mount static files LAST — the "/" catch-all must not shadow any route above.
    if static_arg is not None and static_arg.exists():
        from fastapi.staticfiles import StaticFiles as _StaticFiles
        app.mount(
            "/",
            _StaticFiles(directory=str(static_arg), html=True, check_dir=False),
            name="site",
        )

    url = f"http://{host}:{port}/dashboard?token={token}"
    api_docs = f"http://{host}:{port}/api/docs?token={token}"
    topo_url = f"http://{host}:{port}/topology/clusters?token={token}"

    topo_line = f"\n[bold]Topology:[/bold]  {topo_url}\n" if topology else ""
    tv5_url = f"http://{host}:{port}/api/tv5/live"
    tv5_line = f"[bold]TV-5 3D:[/bold]   {tv5_url}\n" if tv5 else ""
    console.print(Panel(
        _EXPERIMENTAL_BANNER + "\n"
        f"[bold]Listening:[/bold] http://{host}:{port}\n"
        f"[bold]Capsules:[/bold]  {resolved_capsule_dir}\n"
        f"[bold]Registry:[/bold]  {db_path or '~/.novafabric/registry.db'}\n"
        f"\n[bold]Dashboard:[/bold] {url}\n"
        f"[bold]API docs:[/bold]  {api_docs}\n"
        + topo_line
        + tv5_line
        + "\n[dim]Token also written to ~/.novafabric/.serve-token (mode 0600).[/dim]\n"
        "[dim]Press Ctrl+C to stop.[/dim]",
        title="nova serve",
        border_style="green",
    ))

    if not no_browser and static_arg is not None:
        # Open the browser after a short delay so the server has time to bind.
        def _open() -> None:
            time.sleep(0.6)
            try:
                webbrowser.open(url)
            except Exception:  # nosec B110 — non-fatal
                pass
        threading.Thread(target=_open, daemon=True).start()

    import uvicorn

    try:
        uvicorn.run(
            app,
            host=host,
            port=port,
            log_level="warning",
            access_log=False,
        )
    except KeyboardInterrupt:
        pass
    finally:
        # Token file intentionally NOT cleared here: generate_token() reuses it on
        # the next start so process restarts (e.g. docker restart) keep the same
        # token and don't break open browser sessions.
        console.print("\n[dim]nova serve stopped.[/dim]")
