"""ADR-0183 first slice: the serve/ inline-route count is frozen.

The strategic API surface is ``server/`` (routers, OIDC/RBAC, ``/v0``);
``serve/app.py`` inline route growth is frozen — new endpoints land as
``APIRouter`` modules mountable by both apps, not as new inline
``@app.<verb>`` decorators inside ``create_app``.

This guard counts the inline route decorators statically. The count may go
DOWN (routes migrating out into routers) but must never go UP. If this test
fails because you added an inline route to ``serve/app.py``: don't — put it
in a router (see ADR-0183 for the dual-mount pattern).
"""

from __future__ import annotations

import re
from pathlib import Path

SERVE_APP = Path(__file__).resolve().parents[2] / "src" / "novafabric" / "serve" / "app.py"

# Frozen at the ADR-0183 acceptance count (2026-07-16): 184 feature routes
# + 3 grandfathered ADR-0182 ops endpoints (/livez, /readyz, /metrics) that
# merged the same day. Decrease is progress; increase is a violation.
# Ratcheted 187→184 on 2026-07-16: legal-holds group (GET/POST /api/holds,
# POST /api/holds/{hold_id}/release) migrated to serve/routers/holds.py in
# the first ADR-0183 strangler wave.
FROZEN_INLINE_ROUTE_COUNT = 184

_ROUTE_DECORATOR = re.compile(r"@app\.(get|post|put|delete|patch|websocket)\(")


def test_serve_inline_route_count_is_frozen() -> None:
    source = SERVE_APP.read_text(encoding="utf-8")
    count = len(_ROUTE_DECORATOR.findall(source))
    assert count <= FROZEN_INLINE_ROUTE_COUNT, (
        f"serve/app.py has {count} inline routes — the ADR-0183 freeze caps it at "
        f"{FROZEN_INLINE_ROUTE_COUNT}. New endpoints must land as APIRouter modules "
        "mounted by both apps, not inline serve routes."
    )


def test_freeze_constant_tracks_reality() -> None:
    """When routes migrate out, ratchet the constant down with them."""
    source = SERVE_APP.read_text(encoding="utf-8")
    count = len(_ROUTE_DECORATOR.findall(source))
    assert FROZEN_INLINE_ROUTE_COUNT - count <= 20, (
        f"serve/app.py is down to {count} inline routes but the freeze constant is "
        f"still {FROZEN_INLINE_ROUTE_COUNT} — ratchet FROZEN_INLINE_ROUTE_COUNT down "
        "so the guard keeps teeth."
    )
