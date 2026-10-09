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
"""Audit trail for insecure (anonymous-admin) server starts (ADR-0184 D3).

ADR-0184 makes ``--insecure-no-auth`` an explicit opt-out that must leave an
audit trail, not only a log line: a startup warning scrolls away, a
hash-chained audit entry does not.  :func:`record_insecure_start` appends one
``server.insecure_no_auth`` entry to the deployment audit log
(:func:`novafabric.audit.resolve_audit_log_path`: ``NOVAFABRIC_AUDIT_LOG_PATH``,
else the XDG data directory, else ``~/.local/share/novafabric/audit.jsonl``).

Fail closed: if the entry cannot be written, the server refuses to start in
insecure mode (:class:`InsecureModeAuditError`).  The audit entry is the
compensating control for running without authentication; running without
both is not something the operator asked for.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from novafabric.audit import (
    AUDIT_LOG_PATH_ENV,
    AuditEntry,
    AuditEventType,
    AuditLog,
    resolve_audit_log_path,
)

if TYPE_CHECKING:
    from novafabric.server.config import ServerConfig

__all__ = [
    "AUDIT_LOG_PATH_ENV",
    "INSECURE_START_ACTOR",
    "InsecureModeAuditError",
    "insecure_mode_active",
    "record_insecure_start",
]

#: Actor recorded for the entry — the server process itself, not a user.
INSECURE_START_ACTOR = "system:nova-server"


class InsecureModeAuditError(RuntimeError):
    """The insecure-mode audit entry could not be written; startup is refused."""


def insecure_mode_active(config: ServerConfig) -> bool:
    """True when requests are served as anonymous admin (ADR-0184).

    ``insecure_no_auth`` has no effect while OIDC is enabled, so neither the
    warning nor the audit entry applies then.
    """
    return bool(config.insecure_no_auth and not config.oidc.enabled)


def _resolve_audit_log_path(audit_log_path: Path | None) -> Path:
    if audit_log_path is not None:
        return audit_log_path
    return resolve_audit_log_path()


def record_insecure_start(
    config: ServerConfig, *, audit_log_path: Path | None = None
) -> AuditEntry | None:
    """Append the ``server.insecure_no_auth`` audit entry when insecure mode is active.

    Returns the appended entry, or ``None`` when the server is not running
    insecurely (nothing is written).

    Raises:
        InsecureModeAuditError: the audit log could not be written.
    """
    if not insecure_mode_active(config):
        return None
    from novafabric.server.config import _is_loopback_host

    path = _resolve_audit_log_path(audit_log_path)
    try:
        return AuditLog(path).append(
            event_type=AuditEventType.SERVER_INSECURE_NO_AUTH,
            actor=INSECURE_START_ACTOR,
            resource_id=f"nova-server:{config.host}:{config.port}",
            details={
                "bind_host": config.host,
                "port": config.port,
                "loopback": _is_loopback_host(config.host),
                "i_know_this_is_public": config.i_know_this_is_public,
                "effect": "every request is served as anonymous admin",
                "adr": "ADR-0184",
            },
        )
    except OSError as exc:
        raise InsecureModeAuditError(
            f"Refusing to start with --insecure-no-auth: the audit entry could not "
            f"be written to {path} ({exc.strerror or exc}). ADR-0184 requires an "
            f"audit trail for anonymous-admin mode; fix the path or set "
            f"{AUDIT_LOG_PATH_ENV}."
        ) from exc
