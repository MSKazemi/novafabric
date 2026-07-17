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
from novafabric.server.routes.workspaces import router as workspaces_router

logger = logging.getLogger(__name__)


def _get_version() -> str:
    try:
        return importlib_metadata.version("novafabric")
    except importlib_metadata.PackageNotFoundError:
        return "unknown"


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
        yield
        # Shutdown: nothing to tear down for SQLite.

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

    # Self-observability surface (ADR-0182, experimental):
    # /livez, /readyz, /v0/version, /metrics + HTTP request metrics middleware.
    install_server_observability(app, config)

    # Mount resource routers under /v0
    app.include_router(assets_router, prefix="/v0")
    app.include_router(capsules_router, prefix="/v0")
    app.include_router(lineage_router, prefix="/v0")
    app.include_router(replays_router, prefix="/v0")
    app.include_router(evidence_router, prefix="/v0")
    app.include_router(admin_router, prefix="/v0")
    app.include_router(roles_router, prefix="/v0")
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

    # SCIM provisioning (ADR-0139, experimental) — own /scim/v2 prefix per
    # RFC 7644, NOT under /v0. Inert (404) unless server.scim.enabled AND
    # NOVAFABRIC_SCIM_TOKEN are both configured.
    app.include_router(scim_router)

    return app
