"""Step-up (fresh-authentication) gate for destructive actions (ADR-0246 D1).

A stolen-but-valid session should not be enough to destroy evidence. Routes
in the **destructive registry** additionally require the caller's
authentication to be *fresh* — `auth_time`/`iat` within ``max_age_seconds``
— when the deployment enables step-up.

Slice-1 honesty about scope:

- **Off by default** (`step_up.enabled: false`); flipping the server-mode
  default on is deferred until ADR-0228's authorization model lands.
- Freshness is only meaningful for credentials that carry a timestamp
  (OIDC/JWT `auth_time` or `iat`). API keys and the local token carry none
  and are **exempt in this slice** — recorded in the ADR, not hidden.
- The registry is data, not scattered decorators: what counts as
  destructive lives in one frozenset that the ADR-0228 matrix will
  reference.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import Depends, HTTPException, Request

from novafabric.server.auth import AuthContext, verify_token

#: The destructive-action registry (ADR-0246 D1). Additions are reviewed
#: design changes, not incidental edits — each name is asserted at dependency
#: construction so a typo fails at import time, not silently at runtime.
DESTRUCTIVE_ACTIONS = frozenset(
    {
        "role.assign",
        "role.revoke",
        "capsule.delete",
        "capsule.bulk_delete",
    }
)


def require_step_up(
    action: str,
) -> "Callable[..., Awaitable[None]]":
    """Dependency enforcing fresh auth for *action* when step-up is enabled."""
    if action not in DESTRUCTIVE_ACTIONS:
        raise ValueError(
            f"{action!r} is not in the destructive-action registry — add it to "
            "step_up.DESTRUCTIVE_ACTIONS (a reviewed change) before gating a route"
        )

    async def _dep(
        request: Request,
        auth: Annotated[AuthContext, Depends(verify_token)],
    ) -> None:
        cfg = getattr(request.app.state, "config", None)
        step_up = getattr(cfg, "step_up", None)
        if step_up is None or not step_up.enabled:
            return
        if auth.auth_time is None:
            # No freshness signal on this credential class (API key / local
            # token) — exempt in slice 1, per the module docstring.
            return
        age = time.time() - auth.auth_time
        if age > step_up.max_age_seconds:
            raise HTTPException(
                status_code=401,
                detail={
                    "code": "step_up_required",
                    "action": action,
                    "max_age_seconds": step_up.max_age_seconds,
                    "message": (
                        f"{action} requires authentication fresher than "
                        f"{step_up.max_age_seconds}s (yours is {int(age)}s old); "
                        "re-authenticate and retry"
                    ),
                },
            )

    return _dep
