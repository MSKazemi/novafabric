"""``nova api-proxy`` — transparent LLM API proxy for non-Python clients.

Implements ADR-0026 (Path A — promoted from research note 2026-05-09).
Listens on a TCP port and forwards LLM API calls to an upstream URL,
recording each call into the active capsule's ``model-calls.jsonl``.

Use case: capture Claude Code, Cursor, Continue.dev, custom Node/Go/Rust
agents — anything that talks HTTP to an LLM provider and respects the
standard ``*_BASE_URL`` env vars.

Example::

    # Terminal 1: start the proxy
    nova api-proxy \\
      --capsule-dir .novafabric/runs/<run-id> \\
      --listen 127.0.0.1:8765 \\
      --upstream-url https://api.openai.com

    # Terminal 2: point your client at it
    export OPENAI_BASE_URL=http://127.0.0.1:8765
    claude ...   # or cursor, or any non-Python LLM client

The proxy is base-URL-override only — no TLS MITM, no local CA. See
ADR-0026 for the full scope and anti-patterns.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from novafabric.capture._ulid import new_ulid
from novafabric.capture.capsule import CapsuleWriter
from novafabric.proxy.api_proxy import ApiProxy

_console = Console()


def _parse_listen(spec: str) -> tuple[str, int]:
    if ":" not in spec:
        raise typer.BadParameter(
            f"--listen must be host:port (e.g. 127.0.0.1:8765), got {spec!r}",
            param_hint="'--listen'",
        )
    host, _, port_str = spec.rpartition(":")
    if not host:
        host = "127.0.0.1"
    try:
        port = int(port_str)
    except ValueError as exc:
        raise typer.BadParameter(
            f"--listen port must be an integer, got {port_str!r}",
            param_hint="'--listen'",
        ) from exc
    if not (0 < port < 65536):
        raise typer.BadParameter(
            f"--listen port must be in 1..65535, got {port}",
            param_hint="'--listen'",
        )
    return host, port


def api_proxy_cmd(
    capsule_dir: Annotated[
        Path | None,
        typer.Option(
            "--capsule-dir",
            help="Active capsule directory. Falls back to $NOVAFABRIC_CAPSULE_DIR.",
        ),
    ] = None,
    listen: Annotated[
        str,
        typer.Option(
            "--listen",
            help="Listener address as host:port (e.g. 127.0.0.1:8765).",
        ),
    ] = "127.0.0.1:8765",
    upstream_url: Annotated[
        str | None,
        typer.Option(
            "--upstream-url",
            help=(
                "Upstream LLM API base URL "
                "(e.g. https://api.openai.com, https://api.anthropic.com)."
            ),
        ),
    ] = None,
    span_id: Annotated[
        str | None,
        typer.Option(
            "--span-id",
            help=(
                "Parent OTel span id (16 hex chars). Falls back to "
                "$NOVAFABRIC_SPAN_ID, then to '0'*16."
            ),
        ),
    ] = None,
    upstream_timeout: Annotated[
        float,
        typer.Option(
            "--upstream-timeout",
            help="Wall-clock timeout in seconds for each upstream call.",
        ),
    ] = 60.0,
) -> None:
    """Transparent LLM API proxy for non-Python clients.

    Forwards requests to an upstream LLM API and records all calls into
    the active capsule. Use with Claude Code, Cursor, or any
    OpenAI-API-compatible client that supports a base URL override.

    Scope: per session (records until the proxy is stopped with Ctrl+C).

    \b
    Examples:
      # Start proxy pointing at Anthropic API
      nova api-proxy --upstream-url https://api.anthropic.com

      # Configure a client to use the proxy
      ANTHROPIC_BASE_URL=http://localhost:8765 claude

      # Use a custom port
      nova api-proxy --upstream-url https://api.openai.com --listen 127.0.0.1:9090
    """
    if not upstream_url:
        raise typer.BadParameter(
            "--upstream-url is required (e.g. https://api.openai.com).",
            param_hint="'--upstream-url'",
        )

    # Capsule resolution (v0.6.5 ergonomic improvement):
    #   1. --capsule-dir given → use it, auto-create if missing
    #   2. NOVAFABRIC_CAPSULE_DIR set → use it
    #   3. Neither → auto-allocate a fresh ULID dir under
    #      $PWD/.novafabric/runs/ and announce the path
    cap_dir = capsule_dir or _env_path("NOVAFABRIC_CAPSULE_DIR")
    auto_allocated = False
    if cap_dir is None:
        run_id = new_ulid()
        cap_dir = Path.cwd() / ".novafabric" / "runs" / run_id
        auto_allocated = True

    host, port = _parse_listen(listen)
    resolved_span = span_id or os.environ.get("NOVAFABRIC_SPAN_ID") or ("0" * 16)

    # CapsuleWriter.open() handles mkdir + touch idempotently.
    writer = CapsuleWriter(run_id=cap_dir.name, base_dir=cap_dir.parent)
    writer.open()

    if auto_allocated:
        _console.print(
            f"[dim]Allocated capsule: {cap_dir}[/dim]"
        )
    _console.print(
        f"[green]nova api-proxy[/green] listening on "
        f"[bold]http://{host}:{port}[/bold] → {upstream_url}"
    )
    _console.print(f"[dim]Recording to: {cap_dir}[/dim]")

    proxy = ApiProxy(
        listen_host=host,
        listen_port=port,
        upstream_url=upstream_url,
        writer=writer,
        parent_span_id=resolved_span,
        upstream_timeout_s=upstream_timeout,
    )
    raise typer.Exit(code=proxy.run())


def _env_path(name: str) -> Path | None:
    value = os.environ.get(name)
    return Path(value) if value else None
