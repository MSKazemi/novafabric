"""Role-Based Access Control (RBAC) for the NovaFabric REST API.

ADR-0018: four roles — reader, writer, admin, auditor.
Role hierarchy: reader < writer < admin.
auditor is orthogonal: can view audit trails and evidence; cannot write.

Usage:
    from novafabric.server.rbac import require_role, Role

    @router.post("/v0/assets")
    async def create_asset(auth: Annotated[AuthContext, Depends(require_role(Role.writer))]):
        ...
"""

from __future__ import annotations

import enum
import logging
from collections.abc import Callable
from typing import Annotated

from fastapi import Depends
from fastapi.responses import JSONResponse

from novafabric.server.auth import AuthContext, verify_token

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Role definitions
# ---------------------------------------------------------------------------


class Role(str, enum.Enum):
    reader = "reader"
    writer = "writer"
    admin = "admin"
    auditor = "auditor"


# Ordered hierarchy for reader < writer < admin.
# auditor is not in this list — it's checked separately.
_HIERARCHY: list[Role] = [Role.reader, Role.writer, Role.admin]


def _level(role: Role) -> int:
    """Return numeric hierarchy level (higher = more privileged)."""
    try:
        return _HIERARCHY.index(role)
    except ValueError:
        # auditor or any unknown role has no level in the hierarchy
        return -1


def _satisfies(user_roles: list[str], minimum: Role) -> bool:
    """Return True if *user_roles* meets or exceeds *minimum*.

    * ``admin`` satisfies every role check.
    * ``auditor`` only satisfies ``auditor`` checks (orthogonal).
    * Otherwise the numeric hierarchy is used.
    """
    if not user_roles:
        return False

    # admin is a super-role
    if Role.admin in [_as_role(r) for r in user_roles]:
        return True

    if minimum is Role.auditor:
        return any(_as_role(r) is Role.auditor for r in user_roles)

    min_level = _level(minimum)
    for r in user_roles:
        role = _as_role(r)
        if role is not None and _level(role) >= min_level:
            return True
    return False


def _as_role(value: str) -> Role | None:
    try:
        return Role(value)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Dependency factory
# ---------------------------------------------------------------------------


def require_role(minimum: Role) -> Callable[..., AuthContext]:
    """Return a FastAPI dependency that enforces *minimum* role.

    Raises HTTP 401 if not authenticated, HTTP 403 if insufficient privilege.
    """

    async def _check_role(
        auth: Annotated[AuthContext, Depends(verify_token)],
    ) -> AuthContext:
        if not _satisfies(auth.roles, minimum):
            raise _Forbidden(
                f"Role '{minimum.value}' or higher required; "
                f"token has {auth.roles!r}"
            )
        return auth

    return _check_role  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# HTTP 403 exception + handler
# ---------------------------------------------------------------------------


class _Forbidden(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)


async def forbidden_handler(request: object, exc: _Forbidden) -> JSONResponse:
    from novafabric.server.errors import error_response

    return error_response(403, "forbidden", str(exc))
