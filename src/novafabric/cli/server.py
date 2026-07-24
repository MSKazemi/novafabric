"""CLI command group: `nova server`.

Subcommands:
  nova server start          — Start the multi-user REST API server (ADR-0017/ADR-0029).
  nova server issue-token    — Issue an offline ed25519 JWT (ADR-0018).
  nova server revoke-token   — Revoke an offline token by token ID.
  nova server assign-role    — Assign a role to a subject (role_assignments table).
  nova server flush-jwks-cache — Flush the JWKS cache on the running server.
  nova server saml-metadata  — Emit this SP's SAML metadata XML (ADR-0138, experimental).
  nova server api-key create|list|revoke|rotate — Manage first-class API keys (ADR-0193).

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
    insecure_no_auth: Annotated[
        bool,
        typer.Option(
            "--insecure-no-auth",
            help=(
                "INSECURE: disable local-token auth and treat every request "
                "as anonymous admin (pre-ADR-0184 behaviour). "
                "Env: NOVAFABRIC_SERVER_INSECURE_NO_AUTH."
            ),
        ),
    ] = False,
    i_know_this_is_public: Annotated[
        bool,
        typer.Option(
            "--i-know-this-is-public",
            help=(
                "Second confirmation required to combine --insecure-no-auth "
                "with a non-loopback --host (ADR-0184)."
            ),
        ),
    ] = False,
    i_accept_shared_capsule_store: Annotated[
        bool,
        typer.Option(
            "--i-accept-shared-capsule-store",
            help=(
                "Acknowledge that the capsule store is NOT tenant-partitioned "
                "and run more than one organization anyway (ADR-0178). "
                "Env: NOVAFABRIC_SERVER_I_ACCEPT_SHARED_CAPSULE_STORE."
            ),
        ),
    ] = False,
) -> None:
    """Start the multi-user REST API server.

    With OIDC configured, requests authenticate with OIDC JWTs. Without OIDC
    (local mode, ADR-0184) the server requires an auto-generated local bearer
    token — printed at startup and stored at $NOVAFABRIC_HOME/.server-token
    (mode 0600); pin it via NOVAFABRIC_SERVER_TOKEN.

    Scope: run-time (long-running server process).

    \b
    Examples:
      nova server start
      nova server start --host 0.0.0.0 --port 8080
      nova server start --insecure-no-auth   # anonymous admin, loopback only
    """
    import logging

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
        import pydantic

        from novafabric.server.app import create_app
        from novafabric.server.config import InsecureBindError, check_insecure_bind, load_config
    except ImportError as exc:
        typer.echo(
            f"Server module not available: {exc}. "
            "Run: pip install novafabric[server]",
            err=True,
        )
        raise typer.Exit(code=1)

    try:
        cfg = load_config(config)
    except pydantic.ValidationError as exc:
        typer.echo(f"Invalid server config: {exc}", err=True)
        raise typer.Exit(code=2)

    # CLI flags override config / env vars
    if backend is not None:
        cfg.backend = backend
    if host is not None:
        cfg.host = host
    if port is not None:
        cfg.port = port
    if insecure_no_auth:
        cfg.insecure_no_auth = True
    if i_know_this_is_public:
        cfg.i_know_this_is_public = True
    if i_accept_shared_capsule_store:
        cfg.i_accept_shared_capsule_store = True

    # ADR-0184: re-check after CLI overrides (assignment skips model validation).
    try:
        check_insecure_bind(cfg)
    except InsecureBindError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2)

    # Structured startup logging (uvicorn configures its own loggers later).
    if not logging.getLogger().handlers:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
        )
    log = logging.getLogger("novafabric.server")

    # OIDC-enabled deployments authenticate with OIDC JWTs; the local token
    # only exists in no-OIDC local mode (ADR-0184).
    if not cfg.oidc.enabled:
        if cfg.insecure_no_auth:
            log.warning(
                "INSECURE: --insecure-no-auth is set — every request is "
                "anonymous admin. Do not expose this server beyond loopback "
                "(bind host: %s). See ADR-0184.",
                cfg.host,
            )
        else:
            from novafabric.server.local_token import TOKEN_ENV, ensure_local_token

            token, token_path = ensure_local_token()
            cfg.local_token = token
            log.info("Local auth token (OIDC disabled, ADR-0184): %s", token)
            log.info(
                "Pass it on every request: Authorization: Bearer %s "
                "(e.g. curl -H 'Authorization: Bearer %s' http://%s:%d/v0/assets)",
                token,
                token,
                cfg.host,
                cfg.port,
            )
            log.info(
                "Token stored at %s (mode 0600); pin a stable token via %s.",
                token_path,
                TOKEN_ENV,
            )

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
# nova server list-scim-events
# ---------------------------------------------------------------------------


@server_app.command("list-scim-events")
def list_scim_events_cmd(
    subject: Annotated[
        Optional[str],  # noqa: UP007
        typer.Option("--subject", help="Filter the trail to one subject (userName)."),
    ] = None,
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Emit the events as a JSON array for tooling."),
    ] = False,
    db_path: Annotated[
        Optional[Path],  # noqa: UP007
        typer.Option("--db-path", help="SQLite DB path. Defaults to registry default."),
    ] = None,
) -> None:
    """List the append-only SCIM provisioning audit trail (ADR-0139 D5).

    Read-only. For auditors reviewing who was provisioned or deprovisioned, when,
    and every group→role remap. Scope: single server.

    \b
    Examples:
      nova server list-scim-events
      nova server list-scim-events --subject alice@example.com
      nova server list-scim-events --json
    """
    from novafabric.server.scim_store import list_audit_events

    events = list_audit_events(subject=subject, db_path=db_path)

    if as_json:
        import json

        typer.echo(json.dumps(events, indent=2, default=str))
        return

    if not events:
        typer.echo("No SCIM provisioning events recorded.")
        return

    from rich.console import Console
    from rich.table import Table

    table = Table(title="SCIM provisioning audit trail")
    table.add_column("occurred_at", style="cyan", no_wrap=True)
    table.add_column("operation")
    table.add_column("type")
    table.add_column("subject", style="bold")
    table.add_column("roles: before → after")
    for ev in events:
        before = ", ".join(ev["roles_before"]) or "—"
        after = ", ".join(ev["roles_after"]) or "—"
        table.add_row(
            ev["occurred_at"],
            ev["operation"],
            ev["resource_type"],
            ev["subject"],
            f"{before} → {after}",
        )
    # Wide, non-terminal console so long values are not truncated in pipes.
    Console(width=200).print(table)


# ---------------------------------------------------------------------------
# nova server scim-map-group
# ---------------------------------------------------------------------------


@server_app.command("scim-map-group")
def scim_map_group_cmd(
    group: Annotated[
        Optional[str],  # noqa: UP007
        typer.Argument(help="IdP group displayName (omit with --list)."),
    ] = None,
    role: Annotated[
        Optional[str],  # noqa: UP007
        typer.Argument(
            help="RBAC role: reader, writer, admin, auditor, promoter, approver."
        ),
    ] = None,
    remove: Annotated[
        bool,
        typer.Option("--remove", help="Remove the mapping for <group> instead of setting it."),
    ] = False,
    list_mappings: Annotated[
        bool,
        typer.Option(
            "--list",
            help="Print the effective group→role map (config + env overrides) and exit.",
        ),
    ] = False,
    config: Annotated[
        Optional[Path],  # noqa: UP007
        typer.Option(
            "--config",
            help="Server YAML config path. Defaults to ~/.config/novafabric/server.yaml.",
        ),
    ] = None,
) -> None:
    """Declare, --remove, or --list IdP-group → RBAC-role mappings in the server config.

    ADR-0139 D3: SCIM Groups map to RBAC roles via this operator-declared config
    (`scim.group_role_map`, ADR-0029). Membership then grants/revokes the role
    through the /scim/v2/Groups routes. --list reads the effective loaded config;
    add/remove edit the YAML in place (a running server picks the change up on
    restart). Scope: single server.

    \b
    Examples:
      nova server scim-map-group Engineering writer
      nova server scim-map-group SRE-Admins admin
      nova server scim-map-group Engineering --remove
      nova server scim-map-group --list
    """
    import yaml

    from novafabric.server.config import _DEFAULT_CONFIG_PATH, load_config
    from novafabric.server.scim_group_mapping import VALID_SCIM_ROLES

    if list_mappings:
        current = load_config(config).scim.group_role_map
        if not current:
            typer.echo("No group→role mappings configured.")
            return
        width = max(len(g) for g in current)
        for name in sorted(current):
            typer.echo(f"{name.ljust(width)}  ->  {current[name]}")
        return

    if group is None:
        typer.echo("A group displayName is required unless --list is given.", err=True)
        raise typer.Exit(code=1)
    if not remove:
        if role is None:
            typer.echo("A role is required unless --remove or --list is given.", err=True)
            raise typer.Exit(code=1)
        if role not in VALID_SCIM_ROLES:
            roles_str = ", ".join(sorted(VALID_SCIM_ROLES))
            typer.echo(f"Invalid role '{role}'. Choose from: {roles_str}", err=True)
            raise typer.Exit(code=1)

    path = config or _DEFAULT_CONFIG_PATH
    raw = (yaml.safe_load(path.read_text()) or {}) if path.exists() else {}
    scim = raw.get("scim") or {}
    raw["scim"] = scim
    mapping = scim.get("group_role_map") or {}
    scim["group_role_map"] = mapping

    if remove:
        existed = mapping.pop(group, None) is not None
        msg = (
            f"Removed group→role mapping for '{group}'."
            if existed
            else f"No mapping existed for group '{group}'."
        )
    else:
        mapping[group] = role
        msg = f"Mapped group '{group}' → role '{role}'."

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(raw, sort_keys=False))
    typer.echo(msg)


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
# nova server saml-metadata
# ---------------------------------------------------------------------------


@server_app.command("saml-metadata")
def saml_metadata_cmd(
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
) -> None:
    """Emit this SP's SAML metadata XML for IdP registration (ADR-0138).

    Read-only, no side effects. Requires a ``saml:`` block in the server
    config with ``enabled: true``. Prints the metadata XML to stdout so the
    IdP administrator can register NovaFabric as a Service Provider.

    Experimental (ADR-0138 partial slice): SP metadata and config work today;
    live assertion consumption is pending the D5 library license gate.

    Scope: single server.

    \b
    Examples:
      nova server saml-metadata
      nova server saml-metadata --config /etc/novafabric/server.yaml > sp-metadata.xml
    """
    try:
        from novafabric.server.config import load_config
        from novafabric.server.saml import render_sp_metadata
    except ImportError as exc:
        typer.echo(
            f"Server module not available: {exc}. Run: pip install novafabric[server]",
            err=True,
        )
        raise typer.Exit(code=1)

    cfg = load_config(config)
    if cfg.saml is None or not cfg.saml.enabled:
        typer.echo(
            "SAML is not configured: add a `saml:` block with `enabled: true` "
            "to the server config (see docs/operator-guide.md, ADR-0138).",
            err=True,
        )
        raise typer.Exit(code=1)

    typer.echo(render_sp_metadata(cfg.saml), nl=False)


# ---------------------------------------------------------------------------
# nova server api-key create|list|revoke|rotate (ADR-0193 Track 1, experimental)
# ---------------------------------------------------------------------------

api_key_app = typer.Typer(
    name="api-key",
    help=(
        "Manage first-class API keys (experimental, ADR-0193). "
        "Keys are hashed at rest and shown exactly once at creation."
    ),
    no_args_is_help=True,
)
server_app.add_typer(api_key_app)


@api_key_app.command("create")
def api_key_create_cmd(
    owner: Annotated[
        str,
        typer.Option("--owner", help="Owning principal (user or svc:<name>)."),
    ],
    roles: Annotated[
        str,
        typer.Option(
            "--roles",
            help="Comma-separated roles: reader, writer, admin, auditor.",
        ),
    ] = "reader",
    workspace: Annotated[
        Optional[str],  # noqa: UP007
        typer.Option("--workspace", help="Optional workspace scope (ADR-0178)."),
    ] = None,
    expires_in: Annotated[
        Optional[str],  # noqa: UP007
        typer.Option(
            "--expires-in",
            help="Optional key lifetime (e.g. 90d). Default: no expiry.",
        ),
    ] = None,
    created_by: Annotated[
        str,
        typer.Option("--created-by", help="Actor recorded in the audit log."),
    ] = "cli",
    db_path: Annotated[
        Optional[Path],  # noqa: UP007
        typer.Option("--db-path", help="SQLite DB path. Defaults to registry default."),
    ] = None,
) -> None:
    """Create an API key and print it ONCE (experimental, ADR-0193).

    Only the sha256 of the secret is stored; the full key cannot be
    recovered later. The creation is appended to the hash-chained audit log.

    Scope: single server.

    \b
    Examples:
      nova server api-key create --owner alice@example.com --roles reader
      nova server api-key create --owner svc:ci-bot --roles reader,writer --expires-in 90d
    """
    from novafabric.server.api_keys import InvalidRoleError, create_key

    role_list = [r.strip() for r in roles.split(",") if r.strip()]
    days = _parse_days(expires_in) if expires_in else None

    try:
        key, record = create_key(
            owner,
            role_list,
            actor=created_by,
            workspace=workspace,
            expires_in_days=days,
            db_path=db_path,
        )
    except InvalidRoleError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1)
    except Exception as exc:  # noqa: BLE001
        typer.echo(f"Failed to create API key: {exc}", err=True)
        raise typer.Exit(code=1)

    typer.echo(f"API key created (key_id: {record['key_id']}).")
    typer.echo(
        "This key is shown once and cannot be recovered — store it now:"
    )
    typer.echo(key)


@api_key_app.command("list")
def api_key_list_cmd(
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Emit the keys as a JSON array for tooling."),
    ] = False,
    db_path: Annotated[
        Optional[Path],  # noqa: UP007
        typer.Option("--db-path", help="SQLite DB path. Defaults to registry default."),
    ] = None,
) -> None:
    """List API keys — metadata only, never secrets or hashes (ADR-0193).

    Scope: single server.

    \b
    Examples:
      nova server api-key list
      nova server api-key list --json
    """
    from novafabric.server.api_keys import list_keys

    keys = list_keys(db_path=db_path)

    if as_json:
        import json

        typer.echo(json.dumps(keys, indent=2, default=str))
        return

    if not keys:
        typer.echo("No API keys found.")
        return

    from rich.console import Console
    from rich.table import Table

    table = Table(title="API keys (secrets are never stored or shown)")
    table.add_column("key_id", style="bold", no_wrap=True)
    table.add_column("owner")
    table.add_column("roles")
    table.add_column("workspace")
    table.add_column("created_at", style="cyan", no_wrap=True)
    table.add_column("expires_at")
    table.add_column("status")
    for row in keys:
        status = "revoked" if row["revoked_at"] else "active"
        table.add_row(
            row["key_id"],
            row["owner"],
            ", ".join(row["roles"]),
            row["workspace"] or "—",
            row["created_at"],
            row["expires_at"] or "—",
            status,
        )
    # Wide, non-terminal console so long values are not truncated in pipes.
    Console(width=200).print(table)


@api_key_app.command("revoke")
def api_key_revoke_cmd(
    key_id: Annotated[str, typer.Argument(help="Public key_id to revoke.")],
    revoked_by: Annotated[
        str,
        typer.Option("--revoked-by", help="Actor recorded in the audit log."),
    ] = "cli",
    db_path: Annotated[
        Optional[Path],  # noqa: UP007
        typer.Option("--db-path", help="SQLite DB path. Defaults to registry default."),
    ] = None,
) -> None:
    """Revoke an API key by key_id — effective immediately (ADR-0193).

    Verification is a DB lookup, so the key is rejected on the very next
    request. The revocation is appended to the hash-chained audit log.

    Scope: single server.

    \b
    Examples:
      nova server api-key revoke a1b2c3d4
    """
    from novafabric.server.api_keys import UnknownKeyError, revoke_key

    try:
        revoke_key(key_id, actor=revoked_by, db_path=db_path)
    except UnknownKeyError as exc:
        typer.echo(str(exc.args[0]) if exc.args else str(exc), err=True)
        raise typer.Exit(code=1)
    except Exception as exc:  # noqa: BLE001
        typer.echo(f"Failed to revoke API key: {exc}", err=True)
        raise typer.Exit(code=1)

    typer.echo(f"API key '{key_id}' revoked.")


@api_key_app.command("rotate")
def api_key_rotate_cmd(
    key_id: Annotated[str, typer.Argument(help="Public key_id to rotate.")],
    overlap_seconds: Annotated[
        Optional[int],  # noqa: UP007
        typer.Option(
            "--overlap-seconds",
            help=(
                "Overlap window (seconds) during which BOTH keys stay valid. "
                "Default: NOVA_API_KEY_ROTATE_OVERLAP_S or 86400 (24h)."
            ),
        ),
    ] = None,
    rotated_by: Annotated[
        str,
        typer.Option("--rotated-by", help="Actor recorded in the audit log."),
    ] = "cli",
    db_path: Annotated[
        Optional[Path],  # noqa: UP007
        typer.Option("--db-path", help="SQLite DB path. Defaults to registry default."),
    ] = None,
) -> None:
    """Rotate an API key: mint a successor and print it ONCE (ADR-0193 D3).

    The successor inherits the predecessor's owner, roles, workspace, and
    expiry. Both keys stay valid for the overlap window; after it elapses the
    predecessor is auto-revoked (checked at verify time — no background job).
    A zero-downtime credential swap for deployed agents.

    Scope: single server.

    \b
    Examples:
      nova server api-key rotate a1b2c3d4
      nova server api-key rotate a1b2c3d4 --overlap-seconds 3600
    """
    from novafabric.server.api_keys import (
        RevokedKeyError,
        UnknownKeyError,
        rotate_key,
    )

    try:
        key, record = rotate_key(
            key_id,
            actor=rotated_by,
            overlap_seconds=overlap_seconds,
            db_path=db_path,
        )
    except UnknownKeyError as exc:
        typer.echo(str(exc.args[0]) if exc.args else str(exc), err=True)
        raise typer.Exit(code=1)
    except RevokedKeyError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1)
    except Exception as exc:  # noqa: BLE001
        typer.echo(f"Failed to rotate API key: {exc}", err=True)
        raise typer.Exit(code=1)

    overlap = record["rotate_overlap_seconds"]
    typer.echo(
        f"API key rotated (successor key_id: {record['key_id']}). "
        f"Both keys stay valid for the overlap window of {overlap}s "
        f"(predecessor auto-revokes after {record['rotate_expires_at']})."
    )
    typer.echo(
        "This successor key is shown once and cannot be recovered — store it now:"
    )
    typer.echo(key)


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
