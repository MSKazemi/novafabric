"""FastAPI cluster collector app for Evidence Fabric (cap-002, ADR-0066).

Provides:
  POST /capsule  — ingest capsule events via the accumulator backend
  GET  /health   — liveness probe (unauthenticated)

Auth
----
``NOVA_COLLECTOR_TOKEN`` set
    Every ``POST /capsule`` must carry the header ``X-Nova-Token: <value>``, from any
    address, loopback included. The comparison is constant-time.

``NOVA_COLLECTOR_TOKEN`` unset — **local mode**
    The collector serves only a caller that looks local: a loopback peer address, a
    ``localhost``/``127.0.0.1``/``[::1]`` ``Host`` header (DNS-rebinding defence), and
    no proxy header -- ``Forwarded``, ``X-Forwarded-*``, ``X-Real-IP`` or ``Via`` (a
    reverse proxy on this host would otherwise make every remote caller look like
    loopback). Anyone else gets ``403``. A warning is logged at startup.

    **Residual -- a header-less proxy.** A same-host reverse proxy that adds none of
    those headers is indistinguishable from a local caller: nginx's
    ``proxy_pass http://127.0.0.1:<port>`` connects from loopback, sends
    ``Host: 127.0.0.1:<port>`` and no ``X-Forwarded-*`` by default. No check the app
    can make sees the remote client behind it. So the rule is operational, and the
    startup warning says it: set ``NOVA_COLLECTOR_TOKEN`` whenever anything proxies
    to the collector. There is deliberately no "allow unauthenticated local" switch:
    defaulting it on is today's behaviour, defaulting it off would break the zero-setup
    local mode, and an operator who knows a proxy is there already has the stronger
    control -- a token, which turns local mode off entirely.

    Why not refuse to start instead: this module is an ASGI *factory*; whatever
    launches it (``uvicorn --factory``, gunicorn, a test client) owns the socket, so
    the factory cannot see the bind address. Checking each connection holds however
    the app is launched, and keeps the local, no-setup mode the project is built on
    (ADR-0184 draws the same line for ``nova server``: loopback may be convenient,
    anything reachable from the network must authenticate).

Tenant
------
The tenant is a property of the **authenticated principal**, never of the request.
A token authenticates one tenant: ``NOVA_COLLECTOR_TENANT`` (default ``"default"``,
the accumulators' own default). Local mode is the same principal. A request body
``tenant_id`` or an event ``tenant`` that names a different tenant is refused with
``403`` rather than silently rewritten, so a misconfigured client fails loudly.
Serving several tenants means one collector (and one token) per tenant.

Accumulator selection
---------------------
If ``NOVA_CLICKHOUSE_URL`` is set, a :class:`ClickHouseAccumulator` is used.
Otherwise a :class:`DuckDBAccumulator` backed by ``:memory:`` is used.
"""

from __future__ import annotations

import ipaddress
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Final

from fastapi import Depends, FastAPI, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from novafabric.serve.auth import is_localhost_host, token_matches

logger = logging.getLogger(__name__)

TOKEN_ENV: Final[str] = "NOVA_COLLECTOR_TOKEN"
TENANT_ENV: Final[str] = "NOVA_COLLECTOR_TENANT"
TOKEN_HEADER: Final[str] = "X-Nova-Token"
DEFAULT_TENANT: Final[str] = "default"

#: Headers a reverse proxy adds. Their presence means the TCP peer is the proxy, not
#: the caller, so a loopback peer proves nothing about where the request came from.
#: Beyond the client-address headers: ``Via`` (Squid, Varnish, RFC 9110 §7.6.3),
#: ``X-Forwarded-Host``/``-Server`` (Apache ``mod_proxy``), ``X-Forwarded-Proto``/
#: ``-Port`` (Traefik, Envoy, cloud load balancers). A proxy that adds none of them
#: (nginx ``proxy_pass`` defaults) is NOT detected -- see the module docstring.
_PROXY_HEADERS: Final[tuple[str, ...]] = (
    "forwarded", "x-forwarded-for", "x-real-ip", "via",
    "x-forwarded-host", "x-forwarded-proto", "x-forwarded-port", "x-forwarded-server",
)


# ---------------------------------------------------------------------------
# Pydantic request model
# ---------------------------------------------------------------------------


#: The accumulators' token columns: DuckDB ``INTEGER`` (int32), ClickHouse ``Int64``.
_INT32_MAX: Final[int] = 2**31 - 1
_INT64_MAX: Final[int] = 2**63 - 1


class CapsuleEvent(BaseModel):
    """One event in a ``POST /capsule`` batch.

    Only the fields an accumulator writes into a typed column are declared, so a
    malformed one is refused with ``422`` here instead of raising inside the
    accumulator (a ``500``). Any other key is kept and passed through unchanged.
    ``model``/``tokens_in``/``tokens_out`` are the DuckDB columns, ``model_id``/
    ``tokens_total`` the ClickHouse ones. ``tenant`` is checked against the
    principal by the route, not here.
    """

    model_config = ConfigDict(extra="allow")

    event_type: str = Field(min_length=1)
    run_id: str | None = None
    model: str | None = None
    model_id: str | None = None
    tool_name: str | None = None
    tokens_in: int = Field(default=0, ge=0, le=_INT32_MAX)
    tokens_out: int = Field(default=0, ge=0, le=_INT32_MAX)
    tokens_total: int | None = Field(default=None, ge=0, le=_INT64_MAX)

    @field_validator("run_id")
    @classmethod
    def _run_id_not_null(cls, value: str | None) -> str:
        # Absent means "use capsule_id"; an explicit null would reach a NOT NULL column.
        if value is None:
            raise ValueError("run_id must be a string when present")
        return value


class CapsuleIngestionRequest(BaseModel):
    """Request body for ``POST /capsule``.

    ``tenant_id`` is optional and, when present, only an assertion: it must equal
    the authenticated principal's tenant, which is what is stored. Each event is a
    :class:`CapsuleEvent`; one malformed event fails the whole batch with ``422``.
    """

    capsule_id: str
    tenant_id: str | None = None
    events: list[CapsuleEvent]


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CollectorPrincipal:
    """Who a request was authenticated as, and therefore which tenant it writes to."""

    tenant: str
    #: ``"token"`` for a verified ``X-Nova-Token``; ``"local"`` for local mode.
    method: str


def _is_loopback_address(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _is_local_request(request: Request) -> bool:
    """True only for a caller provably on this machine (see the module docstring).

    Fails closed: no peer address, a non-loopback peer, a foreign ``Host`` header or
    any proxy header each make it ``False``.
    """
    client = request.client
    if client is None or not _is_loopback_address(client.host):
        return False
    if not is_localhost_host(request.headers.get("host")):
        return False
    return not any(h in request.headers for h in _PROXY_HEADERS)


def _make_auth_dependency(
    token: str | None, tenant: str
) -> Callable[[Request], Awaitable[CollectorPrincipal]]:
    """Return the FastAPI dependency that authenticates a ``POST /capsule``."""

    async def _authenticate(request: Request) -> CollectorPrincipal:
        if token is None:
            if _is_local_request(request):
                return CollectorPrincipal(tenant=tenant, method="local")
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"{TOKEN_ENV} is not set, so this collector serves loopback "
                    "callers only; set it to accept remote or proxied requests"
                ),
            )
        provided = request.headers.get(TOKEN_HEADER) or ""
        # Constant time (hmac.compare_digest over fixed-length digests): a plain
        # `!=` returns sooner the earlier the first wrong byte, which leaks the token.
        if not token_matches(provided, token):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"Invalid or missing {TOKEN_HEADER}",
            )
        return CollectorPrincipal(tenant=tenant, method="token")

    return _authenticate


def _tenant_mismatch(principal: CollectorPrincipal, claimed: object) -> HTTPException:
    logger.warning(
        "collector_app: refused a request claiming tenant %r for principal tenant %r",
        claimed, principal.tenant,
    )
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="the request names a tenant other than the authenticated one",
    )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def _default_accumulator() -> Any:
    """Create the default accumulator based on environment."""
    clickhouse_url = os.environ.get("NOVA_CLICKHOUSE_URL")
    if clickhouse_url:
        try:
            from novafabric.evidence_fabric.clickhouse_accumulator import (
                ClickHouseAccumulator,
            )

            logger.info(
                "collector_app: using ClickHouseAccumulator (%s)", clickhouse_url
            )
            return ClickHouseAccumulator()
        except ImportError:
            logger.warning(
                "collector_app: NOVA_CLICKHOUSE_URL is set but clickhouse-connect "
                "is not installed; falling back to DuckDBAccumulator"
            )

    from pathlib import Path

    from novafabric.evidence_fabric.duckdb_accumulator import DuckDBAccumulator

    logger.info("collector_app: using DuckDBAccumulator (:memory:)")
    return DuckDBAccumulator(Path(":memory:"))


def create_collector_app(accumulator: Any = None) -> FastAPI:
    """Create and return the cluster collector FastAPI application.

    Reads ``NOVA_COLLECTOR_TOKEN`` and ``NOVA_COLLECTOR_TENANT`` once, here. Without a
    token the app runs in local mode (loopback callers only) and logs a warning.

    Args:
        accumulator: An accumulator instance with ``ingest_events`` /
                     ``ingest_capsule_events`` methods.  If ``None``, the
                     default accumulator is selected based on environment.

    Returns:
        A configured :class:`fastapi.FastAPI` instance.
    """
    if accumulator is None:
        accumulator = _default_accumulator()

    # Resolve the method name: prefer ingest_events (ClickHouse); fall back
    # to ingest_capsule_events (DuckDB).
    if hasattr(accumulator, "ingest_events"):
        _ingest = accumulator.ingest_events
    else:
        _ingest = accumulator.ingest_capsule_events

    collector_token: str | None = os.environ.get(TOKEN_ENV) or None
    tenant = os.environ.get(TENANT_ENV, "").strip() or DEFAULT_TENANT
    if collector_token is None:
        logger.warning(
            "collector_app: %s is not set — running UNAUTHENTICATED in local mode. "
            "Only loopback callers (no proxy headers, localhost Host) are served; "
            "every other request gets 403. Set %s before exposing the collector "
            "on a network, and set %s whenever anything proxies to the collector: "
            "a reverse proxy on this host that adds no X-Forwarded-*/Via header "
            "(nginx proxy_pass defaults) makes every remote caller look local.",
            TOKEN_ENV, TOKEN_ENV, TOKEN_ENV,
            extra={"nova_event": "collector.unauthenticated_local_mode"},
        )
    auth_dep = _make_auth_dependency(collector_token, tenant)

    app = FastAPI(
        title="NovaFabric Cluster Collector",
        description=(
            "Evidence Fabric ingestion endpoint (cap-002, ADR-0066). "
            "Accepts capsule events and routes them to the configured accumulator."
        ),
        version="1.0.0",
    )

    # ------------------------------------------------------------------
    # Routes
    # ------------------------------------------------------------------

    @app.get("/health")
    async def health() -> dict[str, str]:
        """Liveness probe."""
        return {"status": "ok"}

    @app.post("/capsule")
    async def ingest_capsule(
        body: CapsuleIngestionRequest,
        principal: CollectorPrincipal = Depends(auth_dep),  # noqa: B008 — FastAPI idiom
    ) -> dict[str, Any]:
        """Ingest a batch of capsule events.

        Each event is stamped with the authenticated principal's tenant, and with
        ``run_id = capsule_id`` unless it carries its own. A body ``tenant_id`` or an
        event ``tenant`` naming another tenant fails the whole batch with ``403``;
        nothing is ingested. A malformed event (no ``event_type``, a field of the
        wrong type, a token count out of range) fails it with ``422`` before this
        body runs, so nothing is ingested then either.

        Returns:
            ``{"ok": True, "ingested": N}``
        """
        if body.tenant_id is not None and body.tenant_id != principal.tenant:
            raise _tenant_mismatch(principal, body.tenant_id)
        enriched = []
        for event in body.events:
            # exclude_unset: an absent optional field stays absent, as the client sent it.
            enriched_ev = event.model_dump(exclude_unset=True)
            claimed = enriched_ev.get("tenant", principal.tenant)
            if claimed != principal.tenant:
                raise _tenant_mismatch(principal, claimed)
            enriched_ev.setdefault("run_id", body.capsule_id)
            enriched_ev["tenant"] = principal.tenant
            enriched.append(enriched_ev)

        count = _ingest(enriched)
        return {"ok": True, "ingested": count}

    return app
