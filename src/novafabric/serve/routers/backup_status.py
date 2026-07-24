"""Backup-set status dashboard read surface (ADR-0201 P7, ADR-0183 pattern).

``GET /api/infra/backups`` lists the backup archives in the server-configured
backup directory (``NOVA_BACKUP_DIR``) via the pure, read-only
:func:`novafabric.backup.inventory.list_backup_sets`. Like the collector
status endpoint it returns ``{detected: false}`` when nothing is configured or
the directory does not exist, rather than erroring — a dashboard card should
degrade to "not configured", not to a 500.

**Honest scope.** This is a *listing*, not a verification: each entry reflects
what an archive's ``manifest.json`` claims, never whether its members still
hash-match or its signature validates (that is ``nova backup verify`` /
``verify_backup``, which opens and hashes every member). The directory is
taken only from server configuration — never from a request parameter — so
this endpoint cannot be used to probe arbitrary filesystem paths.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request, Response

from novafabric.backup.inventory import DEFAULT_LIMIT, list_backup_sets
from novafabric.serve.http_cache import conditional_json

BACKUP_DIR_ENV = "NOVA_BACKUP_DIR"


def build_backup_status_router(verify_token: Callable[..., Any]) -> APIRouter:
    """Build the backup-status router (``GET /api/infra/backups``)."""
    router = APIRouter(dependencies=[Depends(verify_token)], tags=["infra"])

    @router.get("/api/infra/backups")
    async def backup_status_endpoint(request: Request) -> Response:
        configured = os.environ.get(BACKUP_DIR_ENV, "").strip()
        if not configured:
            payload: dict[str, Any] = {
                "detected": False, "reason": f"{BACKUP_DIR_ENV} is not set"
            }
        else:
            directory = Path(configured).expanduser()
            if not directory.is_dir():
                payload = {
                    "detected": False,
                    "reason": f"{BACKUP_DIR_ENV} does not point to a directory",
                    "directory": str(directory),
                }
            else:
                summaries, truncated = list_backup_sets(directory, limit=DEFAULT_LIMIT)
                payload = {
                    "detected": True,
                    "directory": str(directory),
                    "count": len(summaries),
                    "truncated": truncated,
                    "backups": [s.model_dump(mode="json") for s in summaries],
                }
        # Content ETag lets the InfraTab poll skip re-downloading an unchanged
        # backup listing (S6).
        return conditional_json(request, payload, max_age=15)

    return router
