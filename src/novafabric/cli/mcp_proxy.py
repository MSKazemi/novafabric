"""``nova mcp-proxy`` — transparent proxy for uninstrumented MCP clients.

Implements ADR-0015 §Secondary capture path. Two transport modes:

* **stdio** (default) — spawns an upstream MCP server as a subprocess.
* **HTTP** (v0.6.3 / C-3.4) — listens on a TCP port and forwards
  HTTP POST + SSE to an upstream HTTP MCP server.

Stdio example (Claude Desktop ``claude_desktop_config.json``)::

    {
      "mcpServers": {
        "filesystem": {
          "command": "nova",
          "args": ["mcp-proxy",
                   "--capsule-dir", "/path/to/.novafabric/runs/01HX...",
                   "--",
                   "npx", "-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
        }
      }
    }

HTTP example::

    nova mcp-proxy \\
      --capsule-dir /path/to/.novafabric/runs/01HX... \\
      --listen 127.0.0.1:8765 \\
      --upstream-url http://upstream-mcp.internal:9000/mcp
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Annotated

import typer

from novafabric.capture.capsule import CapsuleWriter
from novafabric.proxy.mcp_proxy import MCPHttpProxy, MCPProxy


def _parse_listen(spec: str) -> tuple[str, int]:
    """Parse ``host:port`` (or just ``:port``). Raises BadParameter on
    malformed input."""
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


def mcp_proxy_cmd(
    ctx: typer.Context,
    command: Annotated[
        list[str] | None,
        typer.Argument(
            help=(
                "Stdio mode: upstream MCP server command line, e.g. "
                "'npx -y @modelcontextprotocol/server-filesystem /tmp'. "
                "Omit when --listen is set."
            )
        ),
    ] = None,
    capsule_dir: Annotated[
        Path | None,
        typer.Option(
            "--capsule-dir",
            help="Active capsule directory. Falls back to $NOVAFABRIC_CAPSULE_DIR.",
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
    listen: Annotated[
        str | None,
        typer.Option(
            "--listen",
            help=(
                "HTTP mode: bind a TCP listener at host:port (e.g. "
                "127.0.0.1:8765). When set, --upstream-url is required "
                "and the trailing COMMAND is ignored."
            ),
        ),
    ] = None,
    upstream_url: Annotated[
        str | None,
        typer.Option(
            "--upstream-url",
            help=(
                "HTTP mode: upstream MCP server URL "
                "(e.g. http://localhost:9000/mcp). Required with --listen."
            ),
        ),
    ] = None,
) -> None:
    """Transparent MCP stdio proxy that records tool exchanges into the active capsule.

    Two transport modes:
      Stdio (default)  Spawns the upstream MCP server as a child process.
      HTTP             Listens on a TCP port (--listen) and forwards to --upstream-url.

    Scope: per session (records until the MCP server exits).

    \b
    Examples:
      # Stdio mode: wrap the filesystem MCP server
      nova mcp-proxy -- npx @modelcontextprotocol/server-filesystem .

      # HTTP mode: forward to an upstream MCP server
      nova mcp-proxy --listen 127.0.0.1:8765 --upstream-url http://upstream:9000/mcp

      # Record into a specific capsule directory
      nova mcp-proxy --capsule-dir .novafabric/runs/01HX... -- python my_mcp_server.py
    """
    # Capsule resolution (v0.6.5 ergonomic improvement, parallel to
    # nova api-proxy): given dir is auto-created if missing; absent
    # both flag and env var means a fresh ULID dir under
    # $PWD/.novafabric/runs/.
    from novafabric.capture._ulid import new_ulid as _new_ulid

    cap_dir = capsule_dir or _env_path("NOVAFABRIC_CAPSULE_DIR")
    if cap_dir is None:
        run_id = _new_ulid()
        cap_dir = Path.cwd() / ".novafabric" / "runs" / run_id

    resolved_span = span_id or os.environ.get("NOVAFABRIC_SPAN_ID") or ("0" * 16)

    writer = CapsuleWriter(run_id=cap_dir.name, base_dir=cap_dir.parent)
    writer.open()  # idempotent mkdir + touch

    # HTTP mode dispatch.
    if listen is not None:
        if not upstream_url:
            raise typer.BadParameter(
                "--upstream-url is required when --listen is set.",
                param_hint="'--upstream-url'",
            )
        if command or ctx.args:
            raise typer.BadParameter(
                "COMMAND must be empty when --listen is set "
                "(stdio mode and HTTP mode are mutually exclusive).",
                param_hint="'COMMAND'",
            )
        host, port = _parse_listen(listen)
        http_proxy = MCPHttpProxy(
            listen_host=host,
            listen_port=port,
            upstream_url=upstream_url,
            writer=writer,
            parent_span_id=resolved_span,
        )
        raise typer.Exit(code=http_proxy.run())

    # Stdio mode (default — preserves v0.5.x behavior).
    upstream_cmd = list(command or []) + list(ctx.args)
    if not upstream_cmd:
        raise typer.BadParameter(
            "COMMAND is required (the upstream MCP server) in stdio mode. "
            "Use --listen + --upstream-url for HTTP mode.",
            param_hint="'COMMAND'",
        )

    proxy = MCPProxy(
        upstream_cmd=upstream_cmd,
        writer=writer,
        parent_span_id=resolved_span,
        client_in=sys.stdin.buffer,
        client_out=sys.stdout.buffer,
    )
    raise typer.Exit(code=proxy.run())


def _env_path(name: str) -> Path | None:
    value = os.environ.get(name)
    return Path(value) if value else None
