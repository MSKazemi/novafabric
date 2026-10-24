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
from pathlib import Path
from typing import Annotated

from fastapi import Depends, Request
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
_HIERARCHY_VALUES: frozenset[str] = frozenset(r.value for r in _HIERARCHY)


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


#: Operator-facing explanation of when a ``role_assignments`` row is consulted.
#: Shared by ``nova server assign-role`` and ``GET /v0/admin/roles`` so the two
#: surfaces never disagree about whether an assignment grants anything (H6).
NOT_IN_FORCE_EXPLANATION = (
    "role_assignments rows are consulted only once the workspace/org model is in "
    "use (at least one scoped membership or service account exists; ADR-0178). "
    "Until then a principal's roles come from its token alone: carry the role in "
    "the OIDC 'nova_roles' (or 'roles') claim, or issue an offline token with "
    "'nova server issue-token --roles ...'."
)


def role_assignments_in_force(*, db_path: Path | None = None) -> bool:
    """True when ``require_role`` would consult ``role_assignments`` rows today.

    Mirrors the gate in :func:`_scoped_satisfies` exactly — the fallback runs only
    when ``workspace_store.feature_in_use()``. A store error answers ``False``:
    the authorization path fails closed on the same error, so "not in force" is
    the truthful report.
    """
    try:
        from novafabric.server import workspace_store

        return workspace_store.feature_in_use(db_path=db_path)
    except Exception:  # noqa: BLE001 — a status probe must not crash its caller
        logger.warning("could not determine whether role assignments are in force")
        return False


#: The highest role a workspace/org membership grants on a route that names no
#: workspace (ADR-0178 §3, amended 2026-10-24). The capsule store is not
#: partitioned by workspace, so a member keeps the data-plane access this slice
#: gives — a workspace ``admin`` reads and writes like a ``writer`` — but the
#: rest of what a membership carries (``admin`` itself, and the orthogonal roles
#: such as ``auditor``) applies only on that workspace's own routes, never
#: server-wide (review server-02).
UNSCOPED_MEMBERSHIP_CAP = Role.writer


def _cap_unscoped(membership_roles: set[str]) -> set[str]:
    """What *membership_roles* grant on a route that names no workspace."""
    cap = _HIERARCHY.index(UNSCOPED_MEMBERSHIP_CAP)
    ranks = [_HIERARCHY.index(Role(r)) for r in membership_roles if r in _HIERARCHY_VALUES]
    return {_HIERARCHY[min(max(ranks), cap)].value} if ranks else set()

#: Path parameter that names the target workspace (``/v0/workspaces/{ws_id}/...``).
WORKSPACE_PATH_PARAM = "ws_id"


def _scoped_satisfies(
    auth: AuthContext, minimum: Role, workspace_id: str | None = None
) -> bool:
    """ADR-0178 (experimental): global-assignment and membership fallback.

    Consulted ONLY after the token's own roles failed to satisfy *minimum* —
    the global-only path above short-circuits identically to pre-ADR-0178
    behavior. Deployments that never create scoped memberships or service
    accounts return False here (``feature_in_use`` is empty), so their 403
    path is byte-identical too. Any store error fails closed (403).

    Applicable bindings (ADR-0178 §3): the subject's global ``role_assignments``;
    on a route that names *workspace_id*, every membership on that workspace and
    on its org; on any other route, the subject's memberships capped at
    :data:`UNSCOPED_MEMBERSHIP_CAP`. A credential with its own role set
    (``auth.inherits_subject_roles`` False) gets no fallback at all.
    """
    if not auth.inherits_subject_roles:
        return False
    try:
        from novafabric.server import rbac_store, workspace_store

        if not workspace_store.feature_in_use():
            return False
        global_roles = set(rbac_store.get_roles(auth.subject))
        if workspace_id is not None:
            scoped = workspace_store.effective_roles(auth.subject, workspace_id)
        else:
            # effective_roles(None) = global rows + every membership, anywhere.
            scoped = _cap_unscoped(workspace_store.effective_roles(auth.subject))
        effective = set(auth.roles) | global_roles | scoped
        return _satisfies(list(effective), minimum)
    except Exception:  # noqa: BLE001 — authorization fallback must fail closed
        logger.exception("workspace-scoped role resolution failed; failing closed")
        return False


def require_role(minimum: Role) -> Callable[..., AuthContext]:
    """Return a FastAPI dependency that enforces *minimum* role.

    Raises HTTP 401 if not authenticated, HTTP 403 if insufficient privilege.

    Role sources, in order (ADR-0178): the token's own roles first (unchanged
    fast path); if those are insufficient and the credential carries no explicit
    role set, the principal's global ``role_assignments`` rows plus the
    memberships that apply to the request (see :func:`_scoped_satisfies`) —
    consulted only when the workspace/org feature is actually in use.
    """

    async def _check_role(
        request: Request,
        auth: Annotated[AuthContext, Depends(verify_token)],
    ) -> AuthContext:
        return check_role(request, auth, minimum)

    return _check_role  # type: ignore[return-value]


def check_role(request: Request, auth: AuthContext, minimum: Role) -> AuthContext:
    """The :func:`require_role` gate as a plain call, for a dependency that adds its own rule.

    Same role sources and the same 403 as ``Depends(require_role(minimum))`` for
    *request* (its target workspace).
    """
    workspace_id = request.path_params.get(WORKSPACE_PATH_PARAM)
    if not _satisfies(auth.roles, minimum) and not _scoped_satisfies(
        auth, minimum, workspace_id if isinstance(workspace_id, str) else None
    ):
        raise _Forbidden(
            f"Role '{minimum.value}' or higher required; token has {auth.roles!r}"
        )
    return auth


# ---------------------------------------------------------------------------
# HTTP 403 exception + handler
# ---------------------------------------------------------------------------


class _Forbidden(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)


async def forbidden_handler(request: object, exc: _Forbidden) -> JSONResponse:
    from novafabric.server.errors import error_response

    return error_response(403, "forbidden", str(exc))
