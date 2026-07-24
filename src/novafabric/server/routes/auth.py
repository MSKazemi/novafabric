"""Auth resource — /v0/auth.

Implements the Device Authorization Grant (RFC 8628) for ``nova login``.

Endpoints:
  POST /v0/auth/device/code    issue a device code
  POST /v0/auth/token          poll for token (client polling flow)
  POST /v0/auth/approve        admin approves a pending device code (demo/testing)
"""

from __future__ import annotations

import secrets
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import jwt
from fastapi import APIRouter

from novafabric.server.errors import BadRequestError
from novafabric.server.rbac import Role

router = APIRouter(prefix="/auth", tags=["auth"])

# Roles a device-code approval may assign. Client-supplied roles are validated
# against this allowlist so an approver can never mint an unknown/elevated role.
_VALID_ROLES = frozenset(r.value for r in Role)

# Hard cap on the in-memory device-code store to bound memory under a flood of
# unauthenticated /device/code calls (the endpoints are only mounted when the
# operator opts into the demo flow, but bound them anyway).
_MAX_DEVICE_CODES = 1024

# In-memory device code store.  Each entry:
#   {
#       "device_code":        str,
#       "user_code":          str,
#       "verification_uri":   str,
#       "expires_at":         float  (monotonic),
#       "interval":           int,
#       "status":             "pending" | "approved" | "expired",
#       "subject":            str | None,
#       "roles":              list[str],
#   }
_DEVICE_CODES: dict[str, dict[str, Any]] = {}
_DC_LOCK = threading.Lock()

# Device code TTL in seconds
_DC_TTL = 300
_INTERVAL = 5


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/device/code", response_model=None)
async def issue_device_code(body: dict[str, Any]) -> dict[str, Any]:
    """Issue a device code for the Device Authorization Grant.

    Request body: ``{"server_url": "http://..."}`` (optional).
    """
    server_url = str(body.get("server_url", "http://localhost:7433"))
    device_code = str(uuid.uuid4())
    user_code = _generate_user_code()
    verification_uri = f"{server_url.rstrip('/')}/v0/auth/approve"

    entry = {
        "device_code": device_code,
        "user_code": user_code,
        "verification_uri": verification_uri,
        "expires_at": time.monotonic() + _DC_TTL,
        "interval": _INTERVAL,
        "status": "pending",
        "subject": None,
        "roles": [],
    }
    with _DC_LOCK:
        _evict_expired_locked()
        if len(_DEVICE_CODES) >= _MAX_DEVICE_CODES:
            raise BadRequestError("device-code store is full; retry shortly")
        _DEVICE_CODES[device_code] = entry

    return {
        "device_code": device_code,
        "user_code": user_code,
        "verification_uri": verification_uri,
        "expires_in": _DC_TTL,
        "interval": _INTERVAL,
    }


@router.post("/token", response_model=None)
async def poll_for_token(body: dict[str, Any]) -> dict[str, Any]:
    """Poll for a token after the user has approved the device code.

    Returns ``{"error": "authorization_pending"}`` until approved.
    Returns the JWT access token once approved.
    """
    device_code = str(body.get("device_code", ""))
    if not device_code:
        raise BadRequestError("device_code is required")

    with _DC_LOCK:
        entry = _DEVICE_CODES.get(device_code)

    if entry is None:
        raise BadRequestError("Unknown device_code")

    if time.monotonic() > entry["expires_at"]:
        with _DC_LOCK:
            if device_code in _DEVICE_CODES:
                _DEVICE_CODES[device_code]["status"] = "expired"
        return {"error": "expired_token"}

    if entry["status"] == "pending":
        return {"error": "authorization_pending"}

    if entry["status"] == "approved":
        # Build a JWT (signed with a symmetric secret for demo; real deployments
        # delegate to the OIDC provider)
        token = _build_demo_jwt(
            subject=entry["subject"] or "device-user",
            roles=entry["roles"],
        )
        refresh_token = str(uuid.uuid4())
        # Clean up
        with _DC_LOCK:
            _DEVICE_CODES.pop(device_code, None)
        return {
            "access_token": token,
            "refresh_token": refresh_token,
            "token_type": "Bearer",
            "expires_in": 3600,
        }

    return {"error": "access_denied"}


@router.post("/approve", response_model=None)
async def approve_device_code(body: dict[str, Any]) -> dict[str, Any]:
    """Admin endpoint to approve a pending device code (testing / demo).

    Request body:
        {
            "user_code": "XXXX-YYYY",
            "subject": "user@example.com",
            "roles": ["reader"]
        }
    """
    user_code = str(body.get("user_code", ""))
    subject = str(body.get("subject", "device-user"))
    roles = [str(r) for r in body.get("roles", ["reader"])]

    if not user_code:
        raise BadRequestError("user_code is required")

    # Never trust caller-supplied roles verbatim — validate against the RBAC
    # allowlist so an approval cannot introduce an unknown or forged role.
    invalid = sorted(set(roles) - _VALID_ROLES)
    if invalid:
        raise BadRequestError(f"unknown role(s): {invalid}")

    with _DC_LOCK:
        matched = None
        for dc, entry in _DEVICE_CODES.items():
            if entry["user_code"] == user_code and entry["status"] == "pending":
                matched = dc
                break
        if matched is None:
            raise BadRequestError(f"No pending device code for user_code '{user_code}'")
        _DEVICE_CODES[matched]["status"] = "approved"
        _DEVICE_CODES[matched]["subject"] = subject
        _DEVICE_CODES[matched]["roles"] = roles

    return {"ok": True, "message": f"Device code for '{user_code}' approved"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _evict_expired_locked() -> None:
    """Drop expired entries. Caller must hold ``_DC_LOCK``."""
    now = time.monotonic()
    stale = [dc for dc, e in _DEVICE_CODES.items() if now > e["expires_at"]]
    for dc in stale:
        _DEVICE_CODES.pop(dc, None)


def _generate_user_code() -> str:
    """Generate a human-readable 8-character user code (4+4 alphanumeric)."""
    import string

    chars = string.ascii_uppercase + string.digits
    part1 = "".join(secrets.choice(chars) for _ in range(4))
    part2 = "".join(secrets.choice(chars) for _ in range(4))
    return f"{part1}-{part2}"


# Per-process random signing secret. The demo device-grant token is never
# honoured by the real verifier (JWKS/RS256 or the opaque local token), so this
# secret only needs to be unguessable within the process lifetime — a random
# value is strictly safer than a shipped constant and needs no configuration.
_DEMO_SECRET = secrets.token_hex(32)


def _build_demo_jwt(subject: str, roles: list[str]) -> str:
    """Build a demo JWT for the device grant flow.

    NOTE: This uses a symmetric secret and is only suitable for local testing.
    Real deployments must delegate token issuance to an OIDC provider.
    """
    import time as _time

    now = int(_time.time())
    payload = {
        "sub": subject,
        "nova_roles": roles,
        "iat": now,
        "exp": now + 3600,
        "iss": "nova-server-demo",
        "aud": "nova-server",
    }
    return jwt.encode(payload, _DEMO_SECRET, algorithm="HS256")
