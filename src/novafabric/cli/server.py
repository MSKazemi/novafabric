"""CLI command group: `nova server`.

Subcommands:
  nova server start          — Start the multi-user REST API server (ADR-0017/ADR-0029).
  nova server issue-token    — Issue an offline ed25519 JWT (ADR-0018).
  nova server revoke-token   — Revoke an offline token by token ID.
  nova server assign-role    — Assign a role to a subject (role_assignments table).
  nova server flush-jwks-cache — Flush the JWKS cache on the running server.

Install the [server] extra to use: pip install novafabric[server]
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Optional

import typer

server_app = typer.Typer(
    name="server",
    help="Manage the multi-user REST API server (Postgres + OIDC).",
    no_args_is_help=True,
)


@server_app.command("start")
def start_cmd(
    config: Annotated[
        Optional[Path],  # noqa: UP007
        typer.Option(
            "--config",
            "-c",
            help=(
                "Path to server YAML config file. "
                "Defaults to ~/.config/novafabric/server.yaml."
            ),
            exists=False,
            file_okay=True,
            dir_okay=False,
        ),
    ] = None,
    backend: Annotated[
        Optional[str],  # noqa: UP007
        typer.Option(
            "--backend",
            help="Storage backend: 'sqlite' (default) or 'postgres'.",
        ),
    ] = None,
    host: Annotated[
        Optional[str],  # noqa: UP007
        typer.Option(
            "--host",
            help="Bind host (default from config or 127.0.0.1).",
        ),
    ] = None,
    port: Annotated[
        Optional[int],  # noqa: UP007
        typer.Option(
            "--port",
            "-p",
            help="Bind port (default from config or 7433).",
        ),
    ] = None,
) -> None:
    """Start the multi-user REST API server with Postgres and OIDC.

    Requires novafabric[server] and a running Postgres instance.
    Set NOVAFABRIC_DB_URL and NOVAFABRIC_OIDC_ISSUER before starting.

    Scope: run-time (long-running server process).

    \b
    Examples:
      nova server start
      nova server start --host 0.0.0.0 --port 8080
    """
    try:
        import uvicorn
    except ImportError:
        typer.echo(
            "uvicorn is not installed. "
            "Run: pip install novafabric[server]",
            err=True,
        )
        raise typer.Exit(code=1)

    try:
        from novafabric.server.app import create_app
        from novafabric.server.config import load_config
    except ImportError as exc:
        typer.echo(
            f"Server module not available: {exc}. "
            "Run: pip install novafabric[server]",
            err=True,
        )
        raise typer.Exit(code=1)

    cfg = load_config(config)

    # CLI flags override config / env vars
    if backend is not None:
        cfg.backend = backend
    if host is not None:
        cfg.host = host
    if port is not None:
        cfg.port = port

    typer.echo(
        f"Starting NovaFabric server on {cfg.host}:{cfg.port} "
        f"[backend={cfg.backend}]"
    )
    typer.echo("API docs: http://{}:{}/docs".format(cfg.host, cfg.port))
    typer.echo("Press Ctrl+C to stop.")

    app = create_app(cfg)
    uvicorn.run(app, host=cfg.host, port=cfg.port)


# ---------------------------------------------------------------------------
# nova server issue-token
# ---------------------------------------------------------------------------


@server_app.command("issue-token")
def issue_token_cmd(
    subject: Annotated[
        str,
        typer.Option("--subject", help="Token subject (email or identifier)."),
    ],
    roles: Annotated[
        str,
        typer.Option(
            "--roles",
            help="Comma-separated list of roles (e.g. reader,writer).",
        ),
    ] = "reader",
    expires_in: Annotated[
        str,
        typer.Option(
            "--expires-in",
            help="Token lifetime (e.g. 90d, 30d). Default: 90d.",
        ),
    ] = "90d",
    key_path: Annotated[
        Optional[Path],  # noqa: UP007
        typer.Option(
            "--key-path",
            help=(
                "Path to ed25519 private key PEM. "
                "Defaults to NOVAFABRIC_OFFLINE_KEY_PATH env var, "
                "or ~/.novafabric/keys/offline-key.pem."
            ),
            exists=False,
        ),
    ] = None,
) -> None:
    """Issue a signed offline JWT for airgapped or Slurm use.

    Generates an Ed25519 keypair if one does not already exist at --key-path.
    Prints the signed token to stdout.

    Scope: single server.

    \b
    Examples:
      nova server issue-token --subject alice@example.com
      nova server issue-token --subject worker-01 --roles reader,writer --expires-in 30d
    """
    resolved_key = _resolve_key_path(key_path)
    role_list = [r.strip() for r in roles.split(",") if r.strip()]
    days = _parse_days(expires_in)

    try:
        from novafabric.server.offline_tokens import generate_keypair, issue_token

        if not resolved_key.exists():
            typer.echo(f"Key not found at {resolved_key}. Generating new keypair …")
            generate_keypair(resolved_key)
            typer.echo(f"Keypair written to {resolved_key} / {resolved_key.with_suffix('.pub')}")

        token = issue_token(
            subject=subject,
            roles=role_list,
            expires_in_days=days,
            key_path=resolved_key,
        )
    except Exception as exc:  # noqa: BLE001
        typer.echo(f"Failed to issue token: {exc}", err=True)
        raise typer.Exit(code=1)

    typer.echo(token)


# ---------------------------------------------------------------------------
# nova server revoke-token
# ---------------------------------------------------------------------------


@server_app.command("revoke-token")
def revoke_token_cmd(
    token_id: Annotated[str, typer.Argument(help="Token ID (jti) to revoke.")],
    key_path: Annotated[
        Optional[Path],  # noqa: UP007
        typer.Option("--key-path", help="Path to ed25519 private key PEM.", exists=False),
    ] = None,
) -> None:
    """Revoke an offline token by its token ID.

    Marks the token as revoked in the server's audit table. The token ID
    (jti) is embedded in the JWT payload.

    Scope: single server.

    \b
    Examples:
      nova server revoke-token <token-jti>
    """
    resolved_key = _resolve_key_path(key_path)

    try:
        from novafabric.server.offline_tokens import revoke_token

        revoke_token(token_id, resolved_key)
    except KeyError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1)
    except Exception as exc:  # noqa: BLE001
        typer.echo(f"Failed to revoke token: {exc}", err=True)
        raise typer.Exit(code=1)

    typer.echo(f"Token '{token_id}' revoked.")


# ---------------------------------------------------------------------------
# nova server assign-role
# ---------------------------------------------------------------------------


@server_app.command("assign-role")
def assign_role_cmd(
    user: Annotated[str, typer.Argument(help="Subject (email or identifier).")],
    role: Annotated[str, typer.Argument(help="Role to assign: reader, writer, admin, auditor.")],
    assigned_by: Annotated[
        str,
        typer.Option("--assigned-by", help="Who is making the assignment."),
    ] = "cli",
    db_path: Annotated[
        Optional[Path],  # noqa: UP007
        typer.Option("--db-path", help="SQLite DB path. Defaults to registry default."),
    ] = None,
) -> None:
    """Assign a role to a user (reader, writer, admin, or auditor).

    Scope: single server.

    \b
    Examples:
      nova server assign-role alice@example.com reader
      nova server assign-role ci-bot writer --assigned-by admin@example.com
    """
    valid_roles = {"reader", "writer", "admin", "auditor"}
    if role not in valid_roles:
        roles_str = ", ".join(sorted(valid_roles))
        typer.echo(f"Invalid role '{role}'. Choose from: {roles_str}", err=True)
        raise typer.Exit(code=1)

    try:
        from novafabric.server.rbac_store import assign_role

        assign_role(user, role, assigned_by, db_path=db_path)
    except Exception as exc:  # noqa: BLE001
        typer.echo(f"Failed to assign role: {exc}", err=True)
        raise typer.Exit(code=1)

    typer.echo(f"Role '{role}' assigned to '{user}'.")


# ---------------------------------------------------------------------------
# nova server revoke-role
# ---------------------------------------------------------------------------


@server_app.command("revoke-role")
def revoke_role_cmd(
    user: Annotated[str, typer.Argument(help="Subject (email or identifier).")],
    role: Annotated[str, typer.Argument(help="Role to revoke: reader, writer, admin, auditor.")],
    db_path: Annotated[
        Optional[Path],  # noqa: UP007
        typer.Option("--db-path", help="SQLite DB path. Defaults to registry default."),
    ] = None,
) -> None:
    """Revoke a user's role on the NovaFabric server.

    The last admin cannot be removed (lockout invariant enforced).

    Scope: single server.

    \b
    Examples:
      nova server revoke-role alice@example.com admin
    """
    try:
        from novafabric.server.rbac_store import LastAdminError, revoke_role

        deleted = revoke_role(user, role, db_path=db_path)
    except LastAdminError as exc:
        typer.echo(f"Refused: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    except Exception as exc:  # noqa: BLE001
        typer.echo(f"Failed to revoke role: {exc}", err=True)
        raise typer.Exit(code=1)

    if not deleted:
        typer.echo(f"No assignment of role '{role}' to '{user}' was found.", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"Role '{role}' revoked from '{user}'.")


# ---------------------------------------------------------------------------
# nova server flush-jwks-cache
# ---------------------------------------------------------------------------


@server_app.command("flush-jwks-cache")
def flush_jwks_cache_cmd(
    server: Annotated[
        str,
        typer.Option("--server", help="Server URL."),
    ] = "http://localhost:7433",
    token: Annotated[
        Optional[str],  # noqa: UP007
        typer.Option("--token", help="Bearer token with admin role."),
    ] = None,
) -> None:
    """Force the running server to re-fetch its JWKS from the OIDC provider.

    Useful after rotating OIDC signing keys. Requires an admin bearer token.

    Scope: single running server.

    \b
    Examples:
      nova server flush-jwks-cache
      nova server flush-jwks-cache --server https://nova.example.com --token $ADMIN_TOKEN
    """
    import os

    import httpx

    server_url = server.rstrip("/")
    bearer: str | None = token or os.environ.get("NOVA_ADMIN_TOKEN")

    if not bearer:
        # Try stored credentials
        try:
            from novafabric.cli.login import get_token

            bearer = get_token(server_url)
        except Exception:  # noqa: BLE001
            pass

    headers: dict[str, str] = {}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"

    try:
        resp = httpx.post(f"{server_url}/v0/admin/flush-jwks", headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        typer.echo(data.get("message", "JWKS cache flushed."))
    except httpx.HTTPStatusError as exc:
        typer.echo(f"HTTP {exc.response.status_code}: {exc.response.text}", err=True)
        raise typer.Exit(code=1)
    except Exception as exc:  # noqa: BLE001
        typer.echo(f"Failed: {exc}", err=True)
        raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _resolve_key_path(key_path: Optional[Path]) -> Path:  # noqa: UP007
    """Resolve the offline key path from arg, env, or default."""
    import os

    if key_path:
        return key_path
    env_val = os.environ.get("NOVAFABRIC_OFFLINE_KEY_PATH")
    if env_val:
        return Path(env_val)
    return Path.home() / ".novafabric" / "keys" / "offline-key.pem"


def _parse_days(value: str) -> int:
    """Parse a duration string like '90d' or '30' into integer days."""
    value = value.strip()
    if value.endswith("d"):
        return int(value[:-1])
    return int(value)
