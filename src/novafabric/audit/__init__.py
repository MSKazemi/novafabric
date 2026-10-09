from pathlib import Path

from ._log import AuditLog
from ._models import AuditEntry, AuditEventType
from ._paths import AUDIT_LOG_PATH_ENV, resolve_audit_log_path

__all__ = [
    "AuditLog",
    "AuditEntry",
    "AuditEventType",
    "AUDIT_LOG_PATH",
    "AUDIT_LOG_PATH_ENV",
    "resolve_audit_log_path",
]


def __getattr__(name: str) -> Path:
    # ``AUDIT_LOG_PATH`` is a deprecated alias resolved on access; see
    # :func:`novafabric.audit._paths.resolve_audit_log_path` for the precedence.
    if name == "AUDIT_LOG_PATH":
        return resolve_audit_log_path()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
