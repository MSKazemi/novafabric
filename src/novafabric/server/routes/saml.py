"""SAML SP browser endpoints — /v0/auth/saml (ADR-0138, experimental partial).

Endpoints:
  GET  /v0/auth/saml/metadata   SP metadata XML for IdP registration (works today)
  GET  /v0/auth/saml/login      SP-initiated SSO entry (302 when opted in, else 501)
  POST /v0/auth/saml/acs        Assertion Consumer Service (verifies when opted in, else 501)

All endpoints return 404 unless the ``server.saml`` config block is present with
``enabled: true`` — absent block ⇒ behavior identical to a build without SAML.

The signxml-backed signature verifier (``server.saml_verify``) cleared the
ADR-0024 license gate the §D5 note described, so login/ACS can now consume
assertions — but **only** when the operator explicitly sets
``server.saml.experimental_acs_enabled: true``. Without that opt-in the endpoints
still refuse with 501 and the ACS never parses the posted XML (the safe default).
Even opted in, a Security-Architect review remains a pre-production blocking
condition (CLAUDE.md). NovaFabric never consumes an assertion without a verified
signature under an XXE-hardened parser.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

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

if TYPE_CHECKING:
    from novafabric.server.saml import AssertionReplayStore

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


def _acs_replay_store(request: Request) -> "AssertionReplayStore":
    """Return the app-scoped assertion replay store (created once, ADR-0138 V8)."""
    from novafabric.server.saml import AssertionReplayStore

    store = getattr(request.app.state, "saml_replay_store", None)
    if store is None:
        store = AssertionReplayStore()
        request.app.state.saml_replay_store = store
    return store


@router.get("/login", response_model=None)
async def sp_initiated_login(request: Request) -> Response:
    """SP-initiated SSO entry point.

    Refuses (501) unless the experimental ACS is opted in (a flow whose assertion
    could never be verified would strand the user at the IdP). When enabled,
    redirects (302) to the IdP SSO URL with a deflated, base64-encoded
    ``SAMLRequest`` (HTTP-Redirect binding).
    """
    saml = _enabled_saml_config(request)
    try:
        require_signature_verifier(saml)
    except SamlVerifierUnavailableError as exc:
        return _d5_refusal(exc)

    import base64
    import urllib.parse
    import zlib
    from datetime import datetime, timezone

    from fastapi.responses import RedirectResponse

    now = datetime.now(timezone.utc)
    request_id = "_" + now.strftime("%Y%m%d%H%M%S%f") + base64.b32encode(
        zlib.crc32(saml.acs_url.encode()).to_bytes(4, "big")
    ).decode().rstrip("=").lower()
    issue_instant = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    authn_request = (
        '<samlp:AuthnRequest xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol" '
        'xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion" '
        f'ID="{request_id}" Version="2.0" IssueInstant="{issue_instant}" '
        'ProtocolBinding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST" '
        f'AssertionConsumerServiceURL="{saml.acs_url}" Destination="{saml.idp.sso_url}">'
        f"<saml:Issuer>{saml.sp_entity_id}</saml:Issuer>"
        "</samlp:AuthnRequest>"
    )
    # HTTP-Redirect binding: raw DEFLATE (no zlib header) + base64 + urlencode.
    deflated = zlib.compress(authn_request.encode())[2:-4]
    saml_request = base64.b64encode(deflated).decode()
    sep = "&" if "?" in saml.idp.sso_url else "?"
    redirect_url = f"{saml.idp.sso_url}{sep}SAMLRequest={urllib.parse.quote(saml_request)}"
    # Remember the request id so the ACS can enforce InResponseTo (V7).
    pending = getattr(request.app.state, "saml_pending_requests", None)
    if pending is None:
        pending = set()
        request.app.state.saml_pending_requests = pending
    pending.add(request_id)
    return RedirectResponse(redirect_url, status_code=302)


@router.post("/acs", response_model=None)
async def assertion_consumer_service(request: Request) -> Response:
    """Assertion Consumer Service (HTTP-POST binding).

    Refuses (501) unless the experimental ACS is opted in. When enabled: verify
    the XML-DSIG signature and parse under an XXE-hardened parser (V1/V2/V10),
    apply the policy rules (V3–V9, V11), map attributes to roles, resolve the
    subject, and mint a bearer token. Any failure rejects with 401 and issues no
    session; error messages never carry assertion contents (the closed audit rule).
    """
    saml = _enabled_saml_config(request)
    try:
        require_signature_verifier(saml)
    except SamlVerifierUnavailableError as exc:
        return _d5_refusal(exc)

    import base64
    import binascii
    from pathlib import Path

    from novafabric.server.routes.auth import _build_demo_jwt
    from novafabric.server.saml import (
        SamlValidationError,
        map_attributes_to_roles,
        resolve_subject,
        validate_assertion_policy,
    )
    from novafabric.server.saml_verify import parse_and_verify_response

    form = await request.form()
    saml_response_b64 = form.get("SAMLResponse")
    if not saml_response_b64 or not isinstance(saml_response_b64, str):
        return error_response(400, "saml_missing_response", "SAMLResponse form field is required")
    try:
        response_xml = base64.b64decode(saml_response_b64, validate=True)
    except (binascii.Error, ValueError):
        return error_response(400, "saml_bad_encoding", "SAMLResponse is not valid base64")

    try:
        idp_cert_pem = Path(saml.idp.x509_cert_path).read_text()
    except OSError:
        return error_response(500, "saml_idp_cert_unreadable", "IdP certificate could not be read")

    pending = getattr(request.app.state, "saml_pending_requests", None)
    try:
        parsed = parse_and_verify_response(response_xml, idp_cert_pem=idp_cert_pem)
        validate_assertion_policy(
            parsed,
            saml,
            replay_store=_acs_replay_store(request),
            pending_request_ids=pending,
        )
        subject = resolve_subject(parsed, saml)
        roles: list[str] = []
        if saml.attribute_role_map is not None:
            roles = map_attributes_to_roles(saml.attribute_role_map, parsed.attributes)
    except SamlValidationError as exc:
        # exc.reason is the redaction-safe machine code (never assertion contents).
        return error_response(
            401, "saml_assertion_rejected", f"SAML assertion rejected: {exc.reason}"
        )

    if pending is not None and parsed.in_response_to:
        pending.discard(parsed.in_response_to)

    token = _build_demo_jwt(subject=subject, roles=roles)
    return JSONResponse(
        {"access_token": token, "token_type": "Bearer", "expires_in": 3600, "roles": roles}
    )
