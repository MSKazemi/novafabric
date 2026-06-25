"""OIDC token validation middleware for the NovaFabric REST API.

ADR-0018: OIDC Device Authorization Grant, JWKS cache with TTL, no-auth local mode.

When ``ServerConfig.oidc.issuer_url`` is empty, auth is disabled and every
request is treated as an anonymous admin (local-mode default).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
import jwt
from fastapi import Request
from fastapi.responses import JSONResponse

from novafabric.server.config import ServerConfig

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# AuthContext
# ---------------------------------------------------------------------------


@dataclass
class AuthContext:
    """Resolved identity extracted from a validated JWT.

    subject: token subject (email / sub claim)
    roles:   list of role strings from the token (e.g. ["reader", "writer"])
    """

    subject: str
    roles: list[str] = field(default_factory=list)

    def has_role(self, role: str) -> bool:
        return role in self.roles


# Anonymous admin used in no-auth local mode.
LOCAL_AUTH = AuthContext(subject="local", roles=["admin"])

# ---------------------------------------------------------------------------
# JWKS cache
# ---------------------------------------------------------------------------


class JwksCache:
    """Fetches and caches JWKS from an OIDC issuer.

    Thread-safe for single-process use via the GIL.  The cache is invalidated
    when a ``kid`` is not found (key rotation) or when the TTL expires.
    """

    def __init__(self, issuer_url: str, ttl_seconds: int = 3600) -> None:
        self._issuer_url = issuer_url.rstrip("/")
        self._ttl = ttl_seconds
        self._keys: dict[str, Any] = {}  # kid → JWK dict
        self._fetched_at: float = float("-inf")  # never fetched → always stale

    @property
    def _jwks_uri(self) -> str:
        return f"{self._issuer_url}/.well-known/jwks.json"

    def _fetch(self) -> None:
        """Fetch JWKS from the issuer and populate the in-memory key map."""
        try:
            resp = httpx.get(self._jwks_uri, timeout=10)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning("JWKS fetch failed for %s: %s", self._jwks_uri, exc)
            return
        keys: dict[str, Any] = {}
        for jwk in data.get("keys", []):
            kid = jwk.get("kid")
            if kid:
                keys[kid] = jwk
            else:
                # Keyless entry — store under empty string for fallback
                keys.setdefault("", jwk)
        self._keys = keys
        self._fetched_at = time.monotonic()
        logger.debug("JWKS cache refreshed: %d key(s)", len(self._keys))

    def _is_stale(self) -> bool:
        return (time.monotonic() - self._fetched_at) > self._ttl

    def get_key(self, kid: str | None) -> dict[str, Any] | None:
        """Return the JWK for *kid*, refreshing if stale or unknown."""
        if self._is_stale():
            self._fetch()
        lookup = kid or ""
        if lookup not in self._keys:
            # Unknown kid — could be key rotation; try one re-fetch.
            self._fetch()
        return self._keys.get(lookup) or (
            self._keys.get("") if not kid else None
        )

    def flush(self) -> None:
        """Force a re-fetch on the next request (e.g. after ``flush-jwks``)."""
        self._fetched_at = float("-inf")
        self._keys = {}


# Module-level singleton; created lazily per-config.
_jwks_cache: JwksCache | None = None


def _get_jwks_cache(config: ServerConfig) -> JwksCache | None:
    global _jwks_cache  # noqa: PLW0603
    if not config.oidc.enabled:
        return None
    if _jwks_cache is None or _jwks_cache._issuer_url != config.oidc.issuer_url.rstrip("/"):
        _jwks_cache = JwksCache(
            config.oidc.issuer_url,
            ttl_seconds=getattr(config.oidc, "jwks_ttl_seconds", 3600),
        )
    return _jwks_cache


# ---------------------------------------------------------------------------
# Token verification
# ---------------------------------------------------------------------------

_AUTH_CONTEXT_KEY = "nova_auth_context"


async def verify_token(request: Request) -> AuthContext:
    """FastAPI dependency that validates the Bearer JWT and returns AuthContext.

    Raises HTTP 401 (as JSONResponse) if the token is missing or invalid.
    OIDC disabled → returns LOCAL_AUTH immediately.
    """
    config: ServerConfig = request.app.state.config

    if not config.oidc.enabled:
        request.state.auth = LOCAL_AUTH
        return LOCAL_AUTH

    # Extract Bearer token
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise _unauthenticated("Missing or malformed Authorization header")

    raw_token = auth_header.removeprefix("Bearer ").strip()

    # Peek at header to extract kid without full verification
    try:
        unverified_header = jwt.get_unverified_header(raw_token)
    except jwt.exceptions.DecodeError as exc:
        raise _unauthenticated(f"Malformed JWT: {exc}")

    kid: str | None = unverified_header.get("kid")
    alg: str = unverified_header.get("alg", "RS256")

    cache = _get_jwks_cache(config)
    if cache is None:
        # Shouldn't reach here; OIDC is supposed to be enabled
        request.state.auth = LOCAL_AUTH
        return LOCAL_AUTH

    jwk = cache.get_key(kid)
    if jwk is None:
        raise _unauthenticated("Unknown key ID — no matching JWKS key")

    # Validate signature + claims
    try:
        public_key = jwt.algorithms.get_default_algorithms()[alg].from_jwk(jwk)
        payload = jwt.decode(
            raw_token,
            key=public_key,
            algorithms=[alg],
            audience=config.oidc.audience,
            issuer=config.oidc.issuer_url.rstrip("/"),
            options={"require": ["exp", "iss", "aud"]},
        )
    except jwt.ExpiredSignatureError:
        raise _unauthenticated("Token has expired")
    except jwt.InvalidAudienceError:
        raise _unauthenticated("Token audience mismatch")
    except jwt.InvalidIssuerError:
        raise _unauthenticated("Token issuer mismatch")
    except jwt.exceptions.PyJWTError as exc:
        raise _unauthenticated(f"Invalid token: {exc}")

    subject: str = payload.get("sub", "")
    # Support both "roles" and "nova_roles" claim names
    raw_roles: Any = payload.get("nova_roles") or payload.get("roles") or []
    roles: list[str] = list(raw_roles) if isinstance(raw_roles, list) else []

    ctx = AuthContext(subject=subject, roles=roles)
    request.state.auth = ctx
    return ctx


class _Unauthenticated(Exception):
    """Sentinel raised by verify_token to produce a 401 JSON response."""

    def __init__(self, message: str) -> None:
        super().__init__(message)


def _unauthenticated(message: str) -> _Unauthenticated:
    return _Unauthenticated(message)


async def unauthenticated_handler(request: Request, exc: _Unauthenticated) -> JSONResponse:
    from novafabric.server.errors import error_response

    return error_response(401, "unauthenticated", str(exc))


# ---------------------------------------------------------------------------
# JWKS flush helper (called by admin endpoint)
# ---------------------------------------------------------------------------


def flush_jwks_cache() -> None:
    """Invalidate the in-memory JWKS cache so the next request re-fetches."""
    if _jwks_cache is not None:
        _jwks_cache.flush()
