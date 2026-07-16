"""SAML SP browser endpoints — /v0/auth/saml (ADR-0138, experimental partial).

Endpoints:
  GET  /v0/auth/saml/metadata   SP metadata XML for IdP registration (works today)
  GET  /v0/auth/saml/login      SP-initiated SSO entry — 501 while the D5 gate is open
  POST /v0/auth/saml/acs        Assertion Consumer Service — 501 while the D5 gate is open

All endpoints return 404 unless the ``server.saml`` config block is present
with ``enabled: true`` — absent block ⇒ behavior identical to a build without
SAML. The login/ACS endpoints refuse with 501 because the XML-signature
library is an open pre-adoption license gate (ADR-0138 §D5): NovaFabric never
consumes an assertion without verified signatures, so until a Tier-A library
clears the ADR-0024 transitive-tree gate, assertion consumption is refused —
signature validation is never skipped. The ACS never parses the posted XML.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from novafabric.server.config import ServerConfig
from novafabric.server.errors import NotFoundError, error_response
from novafabric.server.saml import (
    SamlConfig,
    SamlVerifierUnavailableError,
    render_sp_metadata,
    require_signature_verifier,
)

router = APIRouter(prefix="/auth/saml", tags=["auth"])


def _enabled_saml_config(request: Request) -> SamlConfig:
    """Return the enabled SAML config or raise 404 (backend disabled)."""
    config: ServerConfig = request.app.state.config
    saml = config.saml
    if saml is None or not saml.enabled:
        raise NotFoundError("SAML backend is not configured on this server")
    return saml


def _d5_refusal(exc: SamlVerifierUnavailableError) -> JSONResponse:
    return error_response(501, "saml_not_available", str(exc))


@router.get("/metadata", response_model=None)
async def sp_metadata(request: Request) -> Response:
    """Emit this SP's SAML metadata XML (entity ID, ACS URL, SP cert).

    Read-only, unauthenticated (metadata is public by design), no side effects.
    """
    saml = _enabled_saml_config(request)
    xml = render_sp_metadata(saml)
    return Response(content=xml, media_type="application/samlmetadata+xml")


@router.get("/login", response_model=None)
async def sp_initiated_login(request: Request) -> Response:
    """SP-initiated SSO entry point.

    Refuses (501) while the ADR-0138 D5 library gate is open: starting a flow
    whose assertion could never be verified at the ACS would strand the user
    at the IdP.
    """
    _enabled_saml_config(request)
    try:
        require_signature_verifier()
    except SamlVerifierUnavailableError as exc:
        return _d5_refusal(exc)
    # Unreachable until a D5-gated verifier ships (kept explicit, not dead-ended).
    return error_response(501, "saml_not_available", "SAML login flow not implemented")


@router.post("/acs", response_model=None)
async def assertion_consumer_service(request: Request) -> Response:
    """Assertion Consumer Service (HTTP-POST binding).

    Refuses (501) every assertion while the D5 gate is open. The posted body
    is never parsed: without an XXE-hardened parser and XML-DSIG verification
    (rules V1/V2/V10) NovaFabric does not touch untrusted assertion XML.
    """
    _enabled_saml_config(request)
    try:
        require_signature_verifier()
    except SamlVerifierUnavailableError as exc:
        return _d5_refusal(exc)
    return error_response(501, "saml_not_available", "SAML ACS flow not implemented")
