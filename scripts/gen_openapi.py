#!/usr/bin/env python3
"""Regenerate ``api/openapi.yaml`` from the live NovaFabric server app.

The server-mode OpenAPI spec is generated, not hand-maintained: it is dumped
directly from the FastAPI app built by ``novafabric.server.app.create_app`` so
it always covers exactly the routes the server mounts. Run after adding or
changing any server route:

    uv run python scripts/gen_openapi.py

The running server's ``/api/openapi.json`` is the same document; this file is
the checked-in, reviewable copy. A curated ``info`` block (title, description,
license) is layered over the generated paths/schemas; ``info.version`` tracks
the package version (the API *path* major version stays ``/v0``).
"""

from __future__ import annotations

from importlib.metadata import version as pkg_version
from pathlib import Path

import yaml

from novafabric.server.app import create_app
from novafabric.server.config import ServerConfig

_OUT = Path(__file__).resolve().parent.parent / "api" / "openapi.yaml"

_INFO_DESCRIPTION = (
    "Multi-user REST API for the NovaFabric AI Asset Registry (ADR-0017 / ADR-0029).\n"
    "URL prefix: /v0/ (the API major version; distinct from info.version below).\n"
    "Authentication: Bearer token via the Authorization header (OIDC or the\n"
    "auto-generated local token), or open when auth is disabled with the explicit,\n"
    "audited --insecure-no-auth flag (ADR-0184). /livez and /readyz are always open;\n"
    "/metrics is token-gated.\n"
    "All list endpoints use cursor-based pagination (limit + next_cursor).\n"
    "All error responses use the standard error envelope.\n\n"
    "This file is GENERATED from the server app by scripts/gen_openapi.py — do not\n"
    "edit it by hand. Regenerate after changing any server route."
)


def build_spec() -> dict:
    """Build the server app and return its OpenAPI document with a curated info block."""
    app = create_app(ServerConfig())
    spec = app.openapi()
    spec["info"] = {
        "title": "NovaFabric Multi-User REST API",
        "version": pkg_version("novafabric"),
        "description": _INFO_DESCRIPTION,
        "license": {
            "name": "Apache-2.0",
            "url": "https://www.apache.org/licenses/LICENSE-2.0",
        },
    }
    # ADR-0188 deprecation & sunset policy block. ``currently-deprecated`` is
    # derived from the runtime DEPRECATION_REGISTER so a regeneration can
    # never drift from the code (the drift gate compares all three surfaces).
    from novafabric.server.deprecation import DEPRECATION_REGISTER

    spec["x-deprecation-policy"] = {
        "adr": "design/adr/0188-api-deprecation-sunset-policy.md",
        "register": "docs/api-reference.md#deprecation-register-adr-0188",
        "headers": ["Deprecation", "Sunset", "Link"],
        "minimum-window": "two minor releases",
        "currently-deprecated": sorted(e.route for e in DEPRECATION_REGISTER),
    }
    return spec


def main() -> None:
    spec = build_spec()
    _OUT.write_text(
        yaml.safe_dump(spec, sort_keys=True, default_flow_style=False, allow_unicode=True)
    )
    ops = sum(
        len([m for m in methods if m in ("get", "post", "put", "patch", "delete")])
        for methods in spec.get("paths", {}).values()
    )
    print(f"Wrote {_OUT} — {len(spec.get('paths', {}))} paths, {ops} operations")


if __name__ == "__main__":
    main()
