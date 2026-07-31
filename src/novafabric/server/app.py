"""FastAPI app factory for the NovaFabric multi-user REST API.

ADR-0017: REST over HTTP/1.1, /v0/ prefix, cursor pagination, error envelope.
ADR-0029: Config from YAML + env-var overrides, secrets never in YAML.

Usage:
    from novafabric.server.app import create_app
    from novafabric.server.config import load_config
    app = create_app(load_config())
"""

from __future__ import annotations

import importlib.metadata as importlib_metadata
import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator

from fastapi import FastAPI

from novafabric.server.auth import _Unauthenticated, unauthenticated_handler
from novafabric.server.config import ServerConfig
from novafabric.server.errors import register_handlers
from novafabric.server.observability import install_server_observability
from novafabric.server.quotas import install_quota_enforcement
from novafabric.server.rate_limit import install_rate_limiting
from novafabric.server.rbac import _Forbidden, forbidden_handler
from novafabric.server.routes.admin import router as admin_router
from novafabric.server.routes.api_keys import router as api_keys_router
from novafabric.server.routes.assets import router as assets_router
from novafabric.server.routes.auth import router as auth_router
from novafabric.server.routes.capsules import router as capsules_router
from novafabric.server.routes.evidence import router as evidence_router
from novafabric.server.routes.lineage import router as lineage_router
from novafabric.server.routes.orgs import router as orgs_router
from novafabric.server.routes.replays import router as replays_router
from novafabric.server.routes.roles import router as roles_router
from novafabric.server.routes.saml import router as saml_router
from novafabric.server.routes.scim import (
    _ScimError,
    scim_error_handler,
)
from novafabric.server.routes.scim import (
    router as scim_router,
)
from novafabric.server.routes.seal import router as seal_router
from novafabric.server.routes.service_accounts import router as service_accounts_router
from novafabric.server.routes.suggestions import router as suggestions_router
from novafabric.server.routes.usage import router as usage_router
from novafabric.server.routes.webhooks import router as webhooks_router
from novafabric.server.routes.workspaces import router as workspaces_router

logger = logging.getLogger(__name__)


def _get_version() -> str:
    try:
        return importlib_metadata.version("novafabric")
    except importlib_metadata.PackageNotFoundError:
        return "unknown"


def _install_usage_metering(app: FastAPI, config: ServerConfig) -> None:
    """Install the ADR-0208 ``api_requests`` accumulator middleware.

    Inert (no middleware object exists at all) unless
    ``server.rate_limits.enabled`` AND ``server.usage.metering_enabled`` —
    with the feature off, upgrading changes zero behavior (ADR-0179
    discipline). The hot path never writes the database: requests bump a
    bounded in-process map; a flush writes at most one ledger row per
    workspace per ``usage.flush_interval_s`` (spec §api_requests). Workspace
    attribution per request rides a small bounded TTL cache so the map feed
    is O(1) between membership changes (deliberate coarseness, documented).
    """
    import time
    from collections import OrderedDict
    from pathlib import Path

    from fastapi import Request

    from novafabric.server.usage import (
        ApiRequestAccumulator,
        metering_active,
        resolve_attribution,
    )

    if not metering_active(config):
        return

    db_path = Path(config.db_path) if config.db_path else None
    accumulator = ApiRequestAccumulator(
        max_entries=config.usage.accumulator_max_entries,
        flush_interval_s=config.usage.flush_interval_s,
        rollup_retention_months=config.usage.rollup_retention_months,
        ledger_retention_months=config.usage.ledger_retention_months,
    )
    app.state.usage_accumulator = accumulator

    attribution_cache: OrderedDict[tuple[str, str | None], tuple[float, str]] = (
        OrderedDict()
    )
    cache_ttl = 30.0
    cache_max = 10_000

    def _workspace_for(auth: object) -> str:
        key = (
            str(getattr(auth, "subject", "")),
            getattr(auth, "workspace", None),
        )
        now = time.monotonic()
        hit = attribution_cache.get(key)
        if hit is not None and now < hit[0]:
            return hit[1]
        workspace = resolve_attribution(auth, db_path).workspace  # type: ignore[arg-type]
        attribution_cache[key] = (now + cache_ttl, workspace)
        attribution_cache.move_to_end(key)
        while len(attribution_cache) > cache_max:
            attribution_cache.popitem(last=False)
        return workspace

    @app.middleware("http")
    async def _usage_accounting_middleware(request: Request, call_next: Any) -> Any:
        response = await call_next(request)
        try:
            auth = getattr(request.state, "auth", None)
            if auth is not None:
                accumulator.add(_workspace_for(auth))
                if accumulator.due():
                    accumulator.flush(db_path)
        except Exception:  # noqa: BLE001 — metering never breaks a request
            logger.warning("usage api_requests accounting failed", exc_info=True)
        return response


def create_app(config: ServerConfig) -> FastAPI:
    """Build and return the FastAPI application.

    Pure factory: no side effects beyond constructing the app object.
    The config is attached to ``app.state.config`` so dependency providers
    can retrieve it via ``request.app.state.config``.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        # Startup: initialise SQLite schema eagerly so the first request is fast.
        from pathlib import Path

        from novafabric.registry.store import get_connection, get_db_path, init_schema

        db_path = Path(config.db_path) if config.db_path else get_db_path()

        # ADR-0211 D3 (experimental): fail-closed schema-skew guard — BEFORE
        # init_schema() and the org bootstrap, so a refused server mutates
        # nothing. `behind`/`ahead_or_foreign` raise SchemaSkewError unless
        # NOVAFABRIC_ALLOW_SCHEMA_SKEW=1 downgrades to a structured warning;
        # `unstamped`/`unknown` warn and start (honesty rule, never refuse
        # on ignorance).
        from novafabric.server.schema_skew import enforce_schema_skew_guard

        enforce_schema_skew_guard(
            backend=config.backend,
            target=config.postgres_dsn if config.backend == "postgres" else db_path,
        )

        conn = get_connection(db_path)
        try:
            init_schema(conn)
        finally:
            conn.close()
        # ADR-0178 (experimental): idempotent default org/workspace bootstrap —
        # existing single-team deployments land in a default hierarchy with no
        # operator action. Registry-tier scoping only; no tenant_id/RLS change.
        from novafabric.server import workspace_store

        workspace_store.ensure_default(db_path=db_path)

        # ADR-0208 (experimental): a quota.workspaces block naming a
        # workspace that does not exist is refused at startup with a clear
        # error (spec §Quota enforcement) — only checked when the feature is
        # active, so disabled deployments see zero behavior change. Runs
        # AFTER ensure_default so budgets on the default workspace validate.
        rl = config.rate_limits
        if rl.enabled and rl.quota is not None and rl.quota.workspaces:
            known = {
                w["slug"] for w in workspace_store.list_workspaces(db_path=db_path)
            }
            unknown = sorted(set(rl.quota.workspaces) - known)
            if unknown:
                raise ValueError(
                    f"quota.workspaces names unknown workspace(s) {unknown}; "
                    f"create them first (POST /v0/workspaces) or fix the "
                    f"config (ADR-0208)."
                )

        # ADR-0178 (2026-07-18): the capsule store is shared across orgs, so
        # a genuinely multi-org deployment has no capsule isolation. Refuse
        # unless the operator has acknowledged it. Checked AFTER the default
        # bootstrap so a fresh single-org install is never blocked.
        from novafabric.server.config import check_multi_org_shared_store

        check_multi_org_shared_store(
            config, len(workspace_store.list_orgs(db_path=db_path))
        )

        # ADR-0205 (experimental): webhook delivery dispatcher — started ONLY
        # when server.webhooks.enabled; otherwise no worker thread exists and
        # behavior is byte-identical (spec: master switch off ⇒ inert).
        dispatcher = None
        if config.webhooks.enabled:
            from novafabric.server.webhook_dispatch import (
                DispatchConfig,
                WebhookDispatcher,
            )
            from novafabric.server.webhooks import resolve_wrapping_backend

            dispatcher = WebhookDispatcher(
                db_path=db_path,
                config=DispatchConfig(
                    queue_max=config.webhooks.queue_max,
                    max_attempts=config.webhooks.max_attempts,
                    timeout_s=config.webhooks.timeout_s,
                    retention_days=config.webhooks.delivery_retention_days,
                    retention_rows=config.webhooks.delivery_retention_rows,
                ),
                wrapping_backend=resolve_wrapping_backend(),
            )
            dispatcher.start()
            app.state.webhook_dispatcher = dispatcher
        yield
        # Shutdown: stop the webhook dispatcher (if any); nothing for SQLite.
        if dispatcher is not None:
            dispatcher.stop()
        # ADR-0208: drain the api_requests accumulator at graceful shutdown
        # (spec: flushed on interval AND at shutdown; crash loss is bounded
        # by one interval).
        accumulator = getattr(app.state, "usage_accumulator", None)
        if accumulator is not None:
            accumulator.flush(db_path, force=True)
            from novafabric.server.usage import close_anchors

            close_anchors()

    app = FastAPI(
        title="NovaFabric REST API",
        version=_get_version(),
        description=(
            "Multi-user REST API for the NovaFabric AI Asset Registry. "
            "ADR-0017 / ADR-0029. URL prefix /v0/."
        ),
        docs_url="/docs",
        redoc_url=None,
        openapi_url="/openapi.json",
        lifespan=lifespan,
    )

    # Attach config so dependency providers can reach it
    app.state.config = config

    # ADR-0184: insecure opt-out must be loud — anonymous admin on every request.
    if config.insecure_no_auth and not config.oidc.enabled:
        logger.warning(
            "INSECURE: insecure_no_auth is enabled — every request is treated "
            "as anonymous admin. Do not expose this server beyond loopback "
            "(bind host: %s). See ADR-0184.",
            config.host,
        )

    # Register custom exception handlers (error envelope)
    register_handlers(app)

    # Auth exception handlers
    app.add_exception_handler(_Unauthenticated, unauthenticated_handler)  # type: ignore[arg-type]
    app.add_exception_handler(_Forbidden, forbidden_handler)  # type: ignore[arg-type]
    # SCIM error envelope (RFC 7644 §3.12) — ADR-0139
    app.add_exception_handler(_ScimError, scim_error_handler)  # type: ignore[arg-type]

    # Health check — unauthenticated. Kept byte-for-byte as the compatibility
    # alias for existing probes (ADR-0182 D2); new probes use /livez + /readyz.
    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "ok": True,
            "service": "nova-server",
            "version": _get_version(),
            "backend": config.backend,
        }

    # SEP-1649 MCP Server Card (NF-039 R7) — UNAUTHENTICATED by design: a
    # discovery document exists so a registry or client can learn an
    # endpoint's protocol version, capabilities and auth *before* connecting.
    # Gating it behind the auth it describes would make it undiscoverable.
    # It advertises only non-secret facts, and reports the auth actually in
    # force rather than implying there is none.
    @app.get("/.well-known/mcp.json")
    async def mcp_server_card() -> dict[str, Any]:
        from novafabric.mcp.servercard import build_server_card

        return build_server_card(config).model_dump(mode="json", exclude_none=True)

    # API rate limiting (ADR-0179, experimental) — inert unless
    # server.rate_limits.enabled. Registered BEFORE the observability install:
    # Starlette wraps the last-added middleware outermost, so adding the
    # limiter first keeps the ADR-0182 metrics middleware outside it and 429
    # rejections are counted in nova_http_requests_total.
    install_rate_limiting(app, config)

    # Storage quotas (ADR-0179 second slice, experimental) — warn-then-reject
    # at the capsule-ingest routes. Inert unless rate_limits.enabled AND a
    # quota block with a non-zero limit is configured.
    install_quota_enforcement(app, config)

    # Usage metering (ADR-0208, experimental) — api_requests accumulator
    # middleware. Inert (no middleware installed at all) unless
    # rate_limits.enabled AND usage.metering_enabled — zero behavior change.
    _install_usage_metering(app, config)

    # Self-observability surface (ADR-0182, experimental):
    # /livez, /readyz, /v0/version, /metrics + HTTP request metrics middleware.
    install_server_observability(app, config)

    # Request-ID correlation (P1 operability). Added last so it is the OUTERMOST
    # middleware: the id is set before any inner middleware/handler and echoed on
    # the response. Every log record then carries it via RequestIdFilter.
    from novafabric.server.request_id import install_request_id_middleware

    install_request_id_middleware(app)

    # Mount resource routers under /v0
    app.include_router(assets_router, prefix="/v0")
    app.include_router(capsules_router, prefix="/v0")
    app.include_router(lineage_router, prefix="/v0")
    app.include_router(replays_router, prefix="/v0")
    app.include_router(evidence_router, prefix="/v0")
    app.include_router(admin_router, prefix="/v0")
    app.include_router(roles_router, prefix="/v0")
    # The RFC 8628 device-grant demo flow is unauthenticated scaffolding whose
    # HS256 tokens the real verifier never honours; only mount it when the
    # operator explicitly opts in (config.demo_device_grant, default False) so
    # production never exposes /v0/auth/approve. See ServerConfig.demo_device_grant.
    if config.demo_device_grant:
        app.include_router(auth_router, prefix="/v0")
    app.include_router(saml_router, prefix="/v0")
    app.include_router(suggestions_router, prefix="/v0")
    app.include_router(seal_router, prefix="/v0")
    # ADR-0178 (experimental): workspace/org model + service accounts —
    # additive registry-tier scoping; tenant_id remains the sole RLS key.
    app.include_router(orgs_router, prefix="/v0")
    app.include_router(workspaces_router, prefix="/v0")
    app.include_router(service_accounts_router, prefix="/v0")
    # ADR-0193 (experimental): first-class API keys REST resource — admin-gated
    # create/list/revoke/rotate over the hash-only key store.
    app.include_router(api_keys_router, prefix="/v0")
    # ADR-0205 (experimental): webhook subscription registry — CRUD + ping +
    # delivery log + redeliver. Dispatch is gated on server.webhooks.enabled.
    app.include_router(webhooks_router, prefix="/v0")
    # ADR-0208 (experimental): usage reporting — always mounted (registry
    # reads, ADR-0205 precedent); accounting itself is master-switch gated.
    app.include_router(usage_router, prefix="/v0")

    # SCIM provisioning (ADR-0139, experimental) — own /scim/v2 prefix per
    # RFC 7644, NOT under /v0. Inert (404) unless server.scim.enabled AND
    # NOVAFABRIC_SCIM_TOKEN are both configured.
    app.include_router(scim_router)

    return app
