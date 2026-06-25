"""Central path resolution for all NovaFabric data directories.

All default paths derive from :func:`nova_home`, which reads ``NOVAFABRIC_HOME``
(defaults to ``~/.novafabric``).  Individual paths can still be overridden via
their own env vars.

Environment variables
---------------------
``NOVAFABRIC_HOME``
    Base directory for all internal NovaFabric data.
    Default: ``~/.novafabric``

``NOVAFABRIC_DB_PATH``
    Asset registry SQLite database path.
    Default: ``$NOVAFABRIC_HOME/registry.db``

``NOVAFABRIC_CAPSULE_DIR``
    Default capsule storage directory.
    Default: ``$NOVAFABRIC_HOME/capsules``

``NOVAFABRIC_DASHBOARD_AUDIT_FILE``
    Dashboard mutation audit log path.
    Default: ``$NOVAFABRIC_HOME/dashboard-audit.jsonl``
"""

from __future__ import annotations

import os
from pathlib import Path


def nova_home() -> Path:
    """Root directory for all NovaFabric internal data.

    Override with ``NOVAFABRIC_HOME``; defaults to ``~/.novafabric``.
    """
    env = os.environ.get("NOVAFABRIC_HOME")
    return Path(env) if env else Path.home() / ".novafabric"


def registry_db_path() -> Path:
    """Asset registry SQLite database path.

    Override with ``NOVAFABRIC_DB_PATH``; falls back to
    ``$NOVAFABRIC_HOME/registry.db``.
    """
    env = os.environ.get("NOVAFABRIC_DB_PATH")
    return Path(env) if env else nova_home() / "registry.db"


def serve_token_path() -> Path:
    """One-shot serve token file: ``$NOVAFABRIC_HOME/.serve-token``."""
    return nova_home() / ".serve-token"


def dashboard_audit_path() -> Path:
    """Dashboard mutation audit log path.

    Override with ``NOVAFABRIC_DASHBOARD_AUDIT_FILE``; falls back to
    ``$NOVAFABRIC_HOME/dashboard-audit.jsonl``.
    """
    env = os.environ.get("NOVAFABRIC_DASHBOARD_AUDIT_FILE")
    return Path(env) if env else nova_home() / "dashboard-audit.jsonl"


def default_capsule_dir() -> Path:
    """Default capsule storage directory.

    Override with ``NOVAFABRIC_CAPSULE_DIR``; falls back to
    ``$NOVAFABRIC_HOME/capsules``.
    """
    env = os.environ.get("NOVAFABRIC_CAPSULE_DIR")
    return Path(env) if env else nova_home() / "capsules"


def daemon_run_dir() -> Path:
    """Directory holding the capture-daemon unix socket and pidfile.

    Default: ``$NOVAFABRIC_HOME/run``. Created mode 0700 by the daemon.
    """
    return nova_home() / "run"


def daemon_socket_path() -> Path:
    """Unix socket the warm capture daemon listens on.

    Override with ``NOVAFABRIC_CAPTURE_SOCKET``; defaults to
    ``$NOVAFABRIC_HOME/run/capture.sock``.
    """
    env = os.environ.get("NOVAFABRIC_CAPTURE_SOCKET")
    return Path(env) if env else daemon_run_dir() / "capture.sock"


def spool_dir() -> Path:
    """Directory holding the local event spool the resident drain forwards from.

    ADR-0092 slice C. Override with ``NOVAFABRIC_SPOOL_DIR``; defaults to
    ``$NOVAFABRIC_HOME/spool``.
    """
    env = os.environ.get("NOVAFABRIC_SPOOL_DIR")
    return Path(env) if env else nova_home() / "spool"
