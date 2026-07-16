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
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator

from fastapi import FastAPI

from novafabric.server.auth import _Unauthenticated, unauthenticated_handler
from novafabric.server.config import ServerConfig
from novafabric.server.errors import register_handlers
from novafabric.server.rbac import _Forbidden, forbidden_handler
from novafabric.server.routes.admin import router as admin_router
from novafabric.server.routes.assets import router as assets_router
from novafabric.server.routes.auth import router as auth_router
from novafabric.server.routes.capsules import router as capsules_router
from novafabric.server.routes.evidence import router as evidence_router
from novafabric.server.routes.lineage import router as lineage_router
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
from novafabric.server.routes.suggestions import router as suggestions_router


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

    # Register custom exception handlers (error envelope)
    register_handlers(app)

    # Auth exception handlers
    app.add_exception_handler(_Unauthenticated, unauthenticated_handler)  # type: ignore[arg-type]
    app.add_exception_handler(_Forbidden, forbidden_handler)  # type: ignore[arg-type]
    # SCIM error envelope (RFC 7644 §3.12) — ADR-0139
    app.add_exception_handler(_ScimError, scim_error_handler)  # type: ignore[arg-type]

    # Health check — unauthenticated
    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "ok": True,
            "service": "nova-server",
            "version": _get_version(),
            "backend": config.backend,
        }

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

    # SCIM provisioning (ADR-0139, experimental) — own /scim/v2 prefix per
    # RFC 7644, NOT under /v0. Inert (404) unless server.scim.enabled AND
    # NOVAFABRIC_SCIM_TOKEN are both configured.
    app.include_router(scim_router)

    return app
