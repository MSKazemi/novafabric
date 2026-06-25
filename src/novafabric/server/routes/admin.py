"""Admin resource — /v0/admin.

Implements:
  POST /v0/admin/flush-jwks   invalidate the in-memory JWKS cache
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends

from novafabric.server.auth import AuthContext
from novafabric.server.rbac import Role, require_role

router = APIRouter(prefix="/admin", tags=["admin"])


@router.post("/flush-jwks", response_model=None)
async def flush_jwks(
    _auth: Annotated[AuthContext, Depends(require_role(Role.admin))],
) -> dict[str, Any]:
    """Flush the JWKS cache so the next request re-fetches from the issuer.

    Requires admin role.
    """
    from novafabric.server.auth import flush_jwks_cache

    flush_jwks_cache()
    return {"ok": True, "message": "JWKS cache flushed"}
