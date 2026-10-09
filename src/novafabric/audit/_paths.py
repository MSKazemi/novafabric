"""Where the hash-chained audit log lives — one resolver, evaluated at call time.

Precedence (first match wins; a blank value counts as unset):

1. ``NOVAFABRIC_AUDIT_LOG_PATH`` — explicit file path (deployments, backup/restore).
2. ``$XDG_DATA_HOME/novafabric/audit.jsonl`` — when ``XDG_DATA_HOME`` is set to an
   absolute path (the XDG Base Directory spec says relative values are invalid
   and must be ignored).
3. ``~/.local/share/novafabric/audit.jsonl`` — the historical default, unchanged,
   so an existing user's log keeps being appended to.

``NOVAFABRIC_HOME`` deliberately does **not** move the audit log: pointing a shell at
another data home must not be able to hide the existing trail or silently start a
second hash chain beside it. Moving the log is always an explicit act
(``NOVAFABRIC_AUDIT_LOG_PATH``), which a deployment sets on purpose.

Every reader and writer of the audit log must call :func:`resolve_audit_log_path` at the
moment it needs the path. Binding the path at import time (the old
``AUDIT_LOG_PATH`` constant) silently ignored every override, which is how test
runs appended ``test-user`` entries to a developer's real audit log.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Final

#: Explicit audit-log path override.
AUDIT_LOG_PATH_ENV: Final[str] = "NOVAFABRIC_AUDIT_LOG_PATH"

#: File name used under the XDG data directory.
AUDIT_LOG_FILENAME: Final[str] = "audit.jsonl"


def resolve_audit_log_path() -> Path:
    """Resolve the hash-chained audit log path from the current environment."""
    explicit = os.environ.get(AUDIT_LOG_PATH_ENV, "").strip()
    if explicit:
        return Path(explicit)
    xdg = os.environ.get("XDG_DATA_HOME", "").strip()
    if xdg and Path(xdg).is_absolute():
        return Path(xdg) / "novafabric" / AUDIT_LOG_FILENAME
    return Path.home() / ".local" / "share" / "novafabric" / AUDIT_LOG_FILENAME


def __getattr__(name: str) -> Path:
    # Backward-compatible alias: ``AUDIT_LOG_PATH`` used to be a module constant.
    # It now resolves on every attribute access. Code that binds it at import
    # time (``from ... import AUDIT_LOG_PATH``) still freezes the value — call
    # :func:`resolve_audit_log_path` instead (``tests/audit/test_audit_log_path.py``
    # forbids that import inside ``src/``).
    if name == "AUDIT_LOG_PATH":
        return resolve_audit_log_path()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
