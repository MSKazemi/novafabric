"""Central path resolution for all NovaFabric data directories.

All default paths derive from :func:`nova_home`, which reads ``NOVAFABRIC_HOME``
(defaults to ``~/.novafabric``).  Individual paths can still be overridden via
their own env vars.

Resolution order for every home-relative path: the path's own override env var
(where one exists) > ``$NOVAFABRIC_HOME/<name>`` > ``~/.novafabric/<name>``. Never
write ``Path.home() / ".novafabric"`` in ``src/``: add a helper here instead. The
few deliberately user-global locations (``~/.config/novafabric``, the XDG
``~/.local/share/novafabric`` legacy seal stores, ``./.novafabric`` project
directories) are not under this home and say so where they are defined. The
hash-chained audit log follows this home when ``NOVAFABRIC_HOME`` is set and
falls back to the XDG data directory otherwise
(:func:`novafabric.audit.resolve_audit_log_path`).

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


def keys_dir() -> Path:
    """Local signing-key store: ``$NOVAFABRIC_HOME/keys``."""
    return nova_home() / "keys"


def local_key_path() -> Path:
    """Default evidence-signing key: ``$NOVAFABRIC_HOME/keys/local-key.pem``."""
    return keys_dir() / "local-key.pem"


def evidence_dir() -> Path:
    """Evidence-bundle directory.

    Override with ``NOVAFABRIC_EVIDENCE_DIR`` (blank counts as unset); falls back to
    ``$NOVAFABRIC_HOME/evidence``.
    """
    env = os.environ.get("NOVAFABRIC_EVIDENCE_DIR", "").strip()
    return Path(env) if env else nova_home() / "evidence"


def tokens_path() -> Path:
    """Issued local-token file: ``$NOVAFABRIC_HOME/tokens.jsonl``."""
    return nova_home() / "tokens.jsonl"


def object_store_dir() -> Path:
    """Local object-store WAL directory: ``$NOVAFABRIC_HOME/object_store``."""
    return nova_home() / "object_store"


def collector_health_path() -> Path:
    """Collector health file: ``$NOVAFABRIC_HOME/collector-health.json``."""
    return nova_home() / "collector-health.json"


def metadata_db_path() -> Path:
    """Metadata-store SQLite path.

    Override with ``NOVAFABRIC_DB_PATH``; falls back to
    ``$NOVAFABRIC_HOME/metadata.db``.
    """
    env = os.environ.get("NOVAFABRIC_DB_PATH")
    return Path(env) if env else nova_home() / "metadata.db"


def registry_db_path() -> Path:
    """Asset registry SQLite database path.

    Override with ``NOVAFABRIC_DB_PATH``; falls back to
    ``$NOVAFABRIC_HOME/registry.db``.
    """
    env = os.environ.get("NOVAFABRIC_DB_PATH")
    return Path(env) if env else nova_home() / "registry.db"


def serve_token_path() -> Path:
    """Serve session-token file: ``$NOVAFABRIC_HOME/.serve-token``.

    Persistent, not one-shot: an existing file is reused across restarts and
    outlives the process until something deletes it.
    """
    return nova_home() / ".serve-token"


def server_token_path() -> Path:
    """Local server auth token file (ADR-0184): ``$NOVAFABRIC_HOME/.server-token``."""
    return nova_home() / ".server-token"


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


def project_runs_dir() -> Path:
    """Project-local capsule directory: ``./.novafabric/runs`` under the CWD.

    Deliberately **not** under :func:`nova_home`. It is the default of the
    in-process ``CaptureOrchestrator`` and of ``nova api-proxy`` /
    ``nova mcp-proxy`` (``docs/README.md`` "Default storage paths"), and what
    the framework adapters fall back to when ``NOVAFABRIC_HOME`` is unset. The
    run-id resolver (``cli/_capsule_ref.py``) searches it as a fallback, so a
    bare run id written here is still found.
    """
    return Path.cwd() / ".novafabric" / "runs"


def adapter_default_runs_dir() -> Path:
    """Where a framework adapter writes capsules when given no ``data_dir``.

    ``$NOVAFABRIC_HOME/runs`` when ``NOVAFABRIC_HOME`` is set, otherwise
    :func:`project_runs_dir`. Note ``runs``, not ``capsules`` — this is *not*
    :func:`default_capsule_dir`; it is the documented SDK/adapter default, kept
    for compatibility. The single definition every adapter shares, so the
    run-id resolver's fallback list cannot drift from what the adapters write.

    An *empty* ``NOVAFABRIC_HOME`` is honoured as given (a relative ``runs``),
    exactly as the per-adapter expressions this replaced behaved.
    """
    return Path(os.environ.get("NOVAFABRIC_HOME", str(project_runs_dir().parent))) / "runs"


def dashboards_dir() -> Path:
    """Directory holding ADR-0235 dashboard and widget JSON files.

    Default: ``$NOVAFABRIC_HOME/dashboards``.

    Deliberately **outside any capsule directory**. Capsules are signed
    evidence and stay read-only — the same boundary ADR-0225 D2 drew for its
    query index. Dashboards are user-authored, frequently rewritten, and must
    never end up inside something whose digest is part of a proof.
    """
    return nova_home() / "dashboards"


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
