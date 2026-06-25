"""``nova daemon start|stop|status`` — control the warm capture daemon.

The daemon imports ``novafabric`` once and serves each run from a fork, so the
fleet-scale cold-start of per-run ``nova capture`` is paid only at startup. See
ADR-0092 (extends ADR-0020).
"""

from __future__ import annotations

import os
import socket

import typer
from rich.console import Console

from novafabric._paths import daemon_run_dir, daemon_socket_path
from novafabric.daemon.server import CaptureDaemon

app = typer.Typer(help="Control the warm capture daemon (resident emitter).")
console = Console()


def _socket_alive() -> bool:
    path = daemon_socket_path()
    if not path.exists():
        return False
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(str(path))
        sock.close()
        return True
    except OSError:
        return False


@app.command()
def start(
    max_concurrency: int = typer.Option(
        64, help="Maximum concurrent runs (forked workers)."
    ),
) -> None:
    """Start the daemon in the foreground.

    Use a process supervisor (systemd, a Slurm Prolog, or a K8s DaemonSet) to
    keep it running. Invoke ``novacap <cmd>`` to capture via the warm daemon.
    """
    if _socket_alive():
        console.print("[yellow]capture daemon already running[/yellow]")
        raise typer.Exit(0)
    run_dir = daemon_run_dir()
    run_dir.mkdir(parents=True, exist_ok=True)
    pidfile = run_dir / "capture.pid"
    pidfile.write_text(str(os.getpid()))
    daemon = CaptureDaemon(
        socket_path=daemon_socket_path(), max_concurrency=max_concurrency
    )
    daemon.bind()
    console.print(
        f"[green]capture daemon listening[/green] {daemon_socket_path()} "
        f"(max_concurrency={max_concurrency})"
    )
    try:
        daemon.serve_forever()
    finally:
        daemon.close()
        pidfile.unlink(missing_ok=True)


@app.command()
def stop() -> None:
    """Signal a running daemon to drain and exit (SIGTERM)."""
    pidfile = daemon_run_dir() / "capture.pid"
    if not pidfile.exists():
        console.print("capture daemon not running")
        raise typer.Exit(0)
    pid = int(pidfile.read_text().strip())
    try:
        os.kill(pid, 15)
        console.print(f"sent SIGTERM to capture daemon pid {pid}")
    except ProcessLookupError:
        pidfile.unlink(missing_ok=True)
        console.print("capture daemon not running (stale pidfile removed)")


@app.command()
def status() -> None:
    """Report whether the daemon is reachable."""
    if _socket_alive():
        console.print(f"[green]running[/green] — {daemon_socket_path()}")
    else:
        console.print("[yellow]not running[/yellow]")
