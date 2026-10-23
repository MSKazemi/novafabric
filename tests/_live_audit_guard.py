"""Guard: no test opens the developer's live hash-chained audit log (review r6).

``tests/conftest.py`` installs :func:`install` once per session. It wraps
``AuditLog.__init__`` so that opening any path in :data:`LIVE_AUDIT_LOGS` raises
:class:`LiveAuditLogWriteError` *before* a byte is written, and records the attempt so
the test fails at teardown even when the code under test swallows the exception (the
bypass-notification audit, for one, logs and continues).

The protected paths are computed at import, from the environment the session started
with, so no later ``HOME`` / ``XDG_DATA_HOME`` / ``NOVAFABRIC_AUDIT_LOG_PATH`` override
inside a test can move them:

* ``<real home>/.local/share/novafabric/audit.jsonl`` — the resolver's last-resort
  default, using the account's home from ``pwd`` rather than ``$HOME``;
* whatever :func:`novafabric.audit.resolve_audit_log_path` resolved to when the session
  began, which is the developer's configured log if their shell exports one.

This catches in-process writers. A subprocess launched with a hand-built ``env=`` that
drops both ``NOVAFABRIC_AUDIT_LOG_PATH`` and ``XDG_DATA_HOME`` is out of its reach; the
session-wide hermetic environment in ``conftest.py`` is what covers subprocesses.
"""

from __future__ import annotations

import os
import pwd
import threading
from pathlib import Path
from typing import Any

from novafabric.audit import AuditLog, resolve_audit_log_path


class LiveAuditLogWriteError(AssertionError):
    """A test tried to open the developer's live audit log."""


def _protected_paths() -> tuple[Path, ...]:
    real_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    paths = [real_home / ".local" / "share" / "novafabric" / "audit.jsonl"]
    started_with = resolve_audit_log_path()
    if started_with not in paths:
        paths.append(started_with)
    return tuple(paths)


#: The live audit-log paths of the account running the suite (fixed at import).
LIVE_AUDIT_LOGS: tuple[Path, ...] = _protected_paths()

_RESOLVED: frozenset[Path] = frozenset(p.resolve() for p in LIVE_AUDIT_LOGS)
_violations: list[str] = []
_lock = threading.Lock()


def _is_live(path: Path) -> bool:
    try:
        return Path(path).resolve() in _RESOLVED
    except (OSError, RuntimeError):
        return False


def install() -> None:
    """Wrap ``AuditLog.__init__`` with the live-path check. Idempotent."""
    original = AuditLog.__init__
    if getattr(original, "_live_audit_guard", False):
        return

    def guarded_init(self: AuditLog, path: Path, *args: Any, **kwargs: Any) -> None:
        if _is_live(path):
            message = f"a test opened the live audit log {path}"
            with _lock:
                _violations.append(message)
            raise LiveAuditLogWriteError(message)
        original(self, path, *args, **kwargs)

    guarded_init._live_audit_guard = True  # type: ignore[attr-defined]
    AuditLog.__init__ = guarded_init  # type: ignore[method-assign]


def take_violations() -> list[str]:
    """Return and clear the attempts recorded since the last call."""
    with _lock:
        taken = list(_violations)
        _violations.clear()
    return taken
