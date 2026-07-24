"""Conditional-GET (ETag) + Cache-Control helpers for dashboard read surfaces.

``conditional_json`` turns a computed JSON payload into either a ``304 Not
Modified`` (when the client's ``If-None-Match`` already matches the payload's
content hash) or a ``200`` carrying a strong ``ETag`` and a private
``Cache-Control: max-age`` (S6, ADR-0199). The ETag is a hash of the canonical
payload, so it is *content-addressed*: two byte-identical responses share an
ETag and a client that already holds one skips re-downloading the body. This
cuts bandwidth and client re-render for the polling dashboard without any
server-side cache — the payload is still computed, only its transfer is
elided. Endpoints that are cheap to serialise but expensive to *recompute*
should still keep their own TTL cache; this helper is complementary.

The hash is deterministic: ``json.dumps(payload, sort_keys=True,
separators=(",", ":"))``. Do not use this for responses whose byte
representation legitimately changes every call (e.g. a server timestamp in the
body) — the ETag would never match and you would pay the hash for nothing.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from fastapi import Request, Response
from fastapi.responses import JSONResponse


def compute_etag(payload: Any) -> str:
    """Strong ETag for *payload* — a quoted sha256 of its canonical JSON."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return '"' + hashlib.sha256(canonical).hexdigest() + '"'


def _if_none_match_matches(header: str | None, etag: str) -> bool:
    """True if *header* (a possibly comma-listed If-None-Match) contains *etag*.

    Handles the ``*`` wildcard and tolerates a ``W/`` weak prefix on the
    client's tags (we always emit strong tags, but clients may echo weak ones).
    """
    if not header:
        return False
    if header.strip() == "*":
        return True
    for raw in header.split(","):
        candidate = raw.strip()
        if candidate.startswith("W/"):
            candidate = candidate[2:]
        if candidate == etag:
            return True
    return False


def conditional_json(request: Request, payload: Any, *, max_age: int = 30) -> Response:
    """Return a 304 when the client's ETag matches, else a 200 with ETag.

    *max_age* seconds populates ``Cache-Control: private, max-age=…`` so a
    browser can serve the response from its own cache without a round trip
    until it expires. ``private`` keeps per-user dashboard data out of shared
    proxies.
    """
    etag = compute_etag(payload)
    cache_control = f"private, max-age={max_age}"
    if _if_none_match_matches(request.headers.get("if-none-match"), etag):
        return Response(
            status_code=304,
            headers={"ETag": etag, "Cache-Control": cache_control},
        )
    return JSONResponse(
        content=payload,
        headers={"ETag": etag, "Cache-Control": cache_control},
    )
