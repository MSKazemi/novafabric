"""SEP-1649 MCP Server Card (NF-039, spec `NF-038-039-mcp-2026-conformance.md`).

A Server Card is the discovery document an MCP registry or client fetches from
``/.well-known/mcp.json`` to learn an endpoint's protocol version, capabilities
and auth **without connecting**.

**Generated, never hand-written.** §5 of the spec rejects a static card
explicitly: a hand-maintained document drifts from what the server actually
does, and a discovery document that lies is worse than none — a client trusts
it precisely because it is meant to be authoritative. So the card is built
from the live :class:`ServerConfig`, and the auth block reports the auth that
is *actually in force*, including when that is "none".

Scope guard (spec §2 non-goals): this **advertises** NovaFabric's existing
capture surface. It does not make NovaFabric an MCP server, and it never
changes auth — it only describes it.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

#: MCP revision this card advertises (spec §1 — the 2026-07-28 "stateless" release).
MCP_PROTOCOL_VERSION = "2026-07-28"

#: SEP-1649 document schema version. Tracks the protocol revision.
SCHEMA_VERSION = "2026-07-28"

#: Where `nova serve` publishes the card (SEP-1649, R7).
WELL_KNOWN_PATH = "/.well-known/mcp.json"


class ServerCardAuth(BaseModel):
    """The auth an MCP client must satisfy. Describes; never changes."""

    model_config = ConfigDict(extra="allow")

    type: str = Field(description="oidc | bearer | none")
    issuer: str | None = Field(default=None, description="OIDC issuer, when type=oidc")


class ServerCardEndpoint(BaseModel):
    model_config = ConfigDict(extra="allow")

    transport: str = Field(description="e.g. streamable-http")
    url: str


class ServerCard(BaseModel):
    """SEP-1649 discovery document."""

    model_config = ConfigDict(extra="allow")

    schemaVersion: str = SCHEMA_VERSION  # noqa: N815 — SEP-1649 wire name
    name: str
    description: str
    protocolVersion: str = MCP_PROTOCOL_VERSION  # noqa: N815 — SEP-1649 wire name
    capabilities: dict[str, Any] = Field(default_factory=dict)
    auth: ServerCardAuth
    endpoints: list[ServerCardEndpoint] = Field(default_factory=list)


class ServerCardValidationError(ValueError):
    """A document is not a valid SEP-1649 Server Card."""


def _capabilities() -> dict[str, Any]:
    """Capabilities NovaFabric's capture surface actually advertises.

    ``tasks`` is marked ``extension: true`` because the 2026-07-28 release moved
    Tasks out of core (spec §1). We *detect and capture* a Tasks-bearing message
    (R5) without implementing its semantics, and the card says exactly that
    rather than implying execution support.
    """
    return {
        "tools": {},
        "elicitation": {},
        "tasks": {"extension": True},
    }


def _oidc_issuer(config: Any) -> str | None:
    """The OIDC issuer the server enforces, or ``None``.

    ``ServerConfig`` keeps OIDC in ``config.oidc`` and is on only when issuer and
    audience are both set; reading a nonexistent ``oidc_issuer`` attribute made
    an OIDC server advertise ``bearer`` with no issuer (review misc-2-06). A
    duck-typed object carrying a bare ``oidc_issuer`` is still honoured.
    """
    if config is None:
        return None
    oidc = getattr(config, "oidc", None)
    if oidc is not None:
        if bool(getattr(oidc, "enabled", False)):
            return str(getattr(oidc, "issuer_url", "") or "") or None
        return None
    legacy = getattr(config, "oidc_issuer", None)
    return str(legacy) if legacy else None


def build_server_card(
    config: Any = None,
    *,
    base_url: str | None = None,
) -> ServerCard:
    """Build the card from the live server configuration.

    *config* is a ``ServerConfig``-shaped object (duck-typed so this module
    stays importable without the ``[server]`` extra). When it is ``None`` the
    card still renders, describing the auth a default local `nova serve` enforces
    (a local bearer token, ADR-0184), not a placeholder.

    **Endpoints are advertised only when the operator says where they are.**
    `nova serve` has no ``/mcp`` route (the MCP proxy is a separate process), so a
    card that derived ``http://host:port/mcp`` from the server's own address named
    an endpoint that does not exist. Pass *base_url* (``nova mcp card show
    --base-url``) to publish the MCP endpoint's real root; without it the card
    lists none, which is what is true (review misc-2-06).
    """
    issuer = _oidc_issuer(config)
    insecure = bool(getattr(config, "insecure_no_auth", False)) if config else False

    if issuer:
        auth = ServerCardAuth(type="oidc", issuer=issuer)
    elif insecure:
        # Report "none" rather than omitting the block. A discovery document
        # that stays silent about absent auth invites a client to assume there
        # is some.
        auth = ServerCardAuth(type="none")
    else:
        auth = ServerCardAuth(type="bearer")

    endpoints = (
        [ServerCardEndpoint(transport="streamable-http", url=f"{base_url.rstrip('/')}/mcp")]
        if base_url
        else []
    )

    return ServerCard(
        name="novafabric-capture",
        description=(
            "NovaFabric MCP capture endpoint — proxies and records MCP traffic "
            "as replayable run-capsule evidence."
        ),
        capabilities=_capabilities(),
        auth=auth,
        endpoints=endpoints,
    )


#: Fields SEP-1649 requires. Kept explicit so a validation failure names the
#: missing field rather than surfacing a pydantic traceback.
REQUIRED_FIELDS = (
    "schemaVersion",
    "name",
    "protocolVersion",
    "capabilities",
    "auth",
)


def validate_server_card(document: Any) -> ServerCard:
    """Validate *document* against SEP-1649. Raises on any violation.

    Deliberately strict about *structure* and permissive about *unknown keys*:
    SEP-1649 is an evolving discovery format, so an unrecognised field is
    forward-compatibility, not an error — but a missing required field means a
    client cannot rely on the document, which is exactly what it exists for.
    """
    if not isinstance(document, dict):
        raise ServerCardValidationError(
            f"Server Card must be a JSON object, got {type(document).__name__}"
        )

    missing = [f for f in REQUIRED_FIELDS if f not in document]
    if missing:
        raise ServerCardValidationError(
            f"Server Card is missing required field(s): {', '.join(missing)}"
        )

    auth = document.get("auth")
    if not isinstance(auth, dict) or "type" not in auth:
        raise ServerCardValidationError("Server Card 'auth' must be an object with 'type'")
    if auth["type"] == "oidc" and not auth.get("issuer"):
        raise ServerCardValidationError("auth type 'oidc' requires an 'issuer'")

    if not isinstance(document.get("capabilities"), dict):
        raise ServerCardValidationError("Server Card 'capabilities' must be an object")

    try:
        return ServerCard.model_validate(document)
    except Exception as exc:  # noqa: BLE001 — re-raised as our own error type
        raise ServerCardValidationError(str(exc)) from exc
