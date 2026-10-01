# Copyright 2024 NovaFabric Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Request-time enforcement of the API-key workspace binding (ADR-0294 D2, experimental).

An ADR-0193 API key may carry an ADR-0178 workspace binding. Until now the
binding was attribution only (ADR-0208 metering). With
``server.api_keys.enforce_workspace_binding`` on, a request authenticated by a
**bound** key is refused with 403 when:

- the bound workspace does not exist (``workspace_binding_invalid``) — a
  phantom binding would otherwise attribute usage to an unbudgeted slug; an
  unreadable workspace store fails closed the same way;
- the request declares a different workspace, via the
  ``X-NovaFabric-Workspace`` header or the ``workspace`` query parameter
  (``workspace_binding_mismatch``).

Unbound keys and non-key credentials are untouched. Each refusal is audited to
the house audit log, bounded to one entry per (subject, reason, requested) per
window so a misconfigured client cannot flood it.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from novafabric.server.auth import AuthContext

logger = logging.getLogger(__name__)

__all__ = [
    "AUDIT_ACTION",
    "WORKSPACE_HEADER",
    "WorkspaceBindingError",
    "enforce_key_binding",
    "workspace_binding_handler",
]

#: Optional request header declaring the target workspace (ADR-0294 D2).
WORKSPACE_HEADER = "X-NovaFabric-Workspace"
#: House-audit action recorded for every (window-bounded) refusal.
AUDIT_ACTION = "api_key.binding_refused"

REASON_INVALID = "workspace_binding_invalid"
REASON_MISMATCH = "workspace_binding_mismatch"

_AUDIT_WINDOW_SECONDS = 60.0
_AUDIT_MAX_KEYS = 10_000


class WorkspaceBindingError(Exception):
    """A bound API key was used outside its workspace (→ 403)."""

    def __init__(self, code: str, message: str, details: dict[str, Any]) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


async def workspace_binding_handler(
    request: Request, exc: WorkspaceBindingError
) -> JSONResponse:
    """403 with the ADR-0017 envelope."""
    from novafabric.server.errors import error_response  # noqa: PLC0415

    return error_response(403, exc.code, str(exc), exc.details)


class _AuditWindow:
    """LRU-bounded ``key → first-seen`` map: one audit entry per key per window."""

    def __init__(
        self,
        *,
        window_seconds: float = _AUDIT_WINDOW_SECONDS,
        max_keys: int = _AUDIT_MAX_KEYS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._window = window_seconds
        self._max = max_keys
        self._clock = clock
        self._seen: OrderedDict[tuple[str, ...], float] = OrderedDict()
        self._lock = threading.Lock()

    def should_emit(self, key: tuple[str, ...]) -> bool:
        now = self._clock()
        with self._lock:
            first = self._seen.get(key)
            if first is not None and (now - first) <= self._window:
                self._seen.move_to_end(key)
                return False
            self._seen[key] = now
            self._seen.move_to_end(key)
            while len(self._seen) > self._max:
                self._seen.popitem(last=False)
            return True

    def clear(self) -> None:
        with self._lock:
            self._seen.clear()


_audit_window = _AuditWindow()


def _audit_refusal(reason: str, details: dict[str, Any], subject: str) -> None:
    logger.warning(
        "API-key workspace binding refused (%s): subject=%s binding=%s requested=%s",
        reason,
        subject,
        details.get("binding"),
        details.get("requested"),
    )
    key = (subject, reason, str(details.get("requested")))
    if not _audit_window.should_emit(key):
        return
    try:
        from novafabric.serve import audit  # noqa: PLC0415

        audit.append(
            action=AUDIT_ACTION,
            args={"reason": reason, "subject": subject, **details},
            cli_equivalent="n/a (server API-key binding enforcement, ADR-0294)",
            actor_token_fp="api_key",
            result="refused",
        )
    except Exception:  # noqa: BLE001 — auditing must never change the outcome
        logger.warning("API-key binding audit emission failed", exc_info=True)


def _workspace_exists(slug: str, db_path: Path | None) -> bool:
    """True iff *slug* names an ADR-0178 workspace. Store errors → False (fail closed)."""
    from novafabric.server import workspace_store  # noqa: PLC0415

    try:
        conn = workspace_store._get_conn(db_path)  # noqa: SLF001 — shared store helper
    except Exception:  # noqa: BLE001
        logger.warning("workspace store unavailable for binding check", exc_info=True)
        return False
    try:
        row = conn.execute("SELECT 1 FROM workspaces WHERE slug = ? LIMIT 1", (slug,)).fetchone()
        return row is not None
    except sqlite3.Error:
        logger.warning("workspace lookup failed for binding check", exc_info=True)
        return False
    finally:
        conn.close()


def enforce_key_binding(request: Request, ctx: AuthContext) -> None:
    """Refuse a bound key used outside its workspace; no-op unless enabled.

    Called from API-key verification (``server.auth``) after the key is
    resolved, so it guards every route uniformly.

    Raises:
        WorkspaceBindingError: binding unknown, or a declared workspace differs.
    """
    config = getattr(request.app.state, "config", None)
    if config is None or not config.api_keys.enforce_workspace_binding:
        return
    binding = ctx.workspace
    if not binding:
        return
    db_path = Path(config.db_path) if config.db_path else None
    if not _workspace_exists(binding, db_path):
        details = {"binding": binding, "requested": None, "source": "key"}
        _audit_refusal(REASON_INVALID, details, ctx.subject)
        raise WorkspaceBindingError(
            REASON_INVALID,
            f"API key is bound to workspace {binding!r}, which does not exist; "
            "rebind or rotate the key (ADR-0294)",
            details,
        )
    declared: list[tuple[str, str]] = []
    header = request.headers.get(WORKSPACE_HEADER)
    if header is not None:
        declared.append(("header", header.strip()))
    query = request.query_params.get("workspace")
    if query is not None:
        declared.append(("query", query.strip()))
    for source, requested in declared:
        if requested != binding:
            details = {"binding": binding, "requested": requested, "source": source}
            _audit_refusal(REASON_MISMATCH, details, ctx.subject)
            raise WorkspaceBindingError(
                REASON_MISMATCH,
                f"API key is bound to workspace {binding!r} but the request targets "
                f"{requested!r} ({source}) (ADR-0294)",
                details,
            )
