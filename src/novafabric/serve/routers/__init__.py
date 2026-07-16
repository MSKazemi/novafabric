"""Route groups migrated out of the ``serve/app.py`` monolith (ADR-0183).

Each module exposes a ``build_<group>_router(verify_token, ...)`` factory that
returns an ``APIRouter`` with behavior byte-identical to the inline routes it
replaced. The auth dependency is injected by the caller because ``serve``'s
token verification is a closure over the per-process shared token; ``server/``
can mount the same routers behind OIDC/RBAC by passing its own dependency.
"""

from __future__ import annotations
