"""Importable ASGI app factory for multi-worker uvicorn deployments.

``uvicorn --workers N`` spawns fresh worker *processes*; they cannot receive
an in-memory :class:`~novafabric.server.config.ServerConfig` from the parent,
so each worker rebuilds it from the same config file + ``NOVAFABRIC_SERVER_*``
environment the CLI resolved. ``nova server start --workers`` exports the
resolved primitives (host/port/backend/insecure flags) to the environment and
launches ``novafabric.server.factory:make_app`` as a uvicorn factory, so every
worker's :func:`make_app` reconstructs a byte-identical config.

Secrets (``NOVAFABRIC_POSTGRES_DSN``, ``NOVAFABRIC_OFFLINE_KEY_PATH``) are
env-only by design and are inherited by workers without ever being written to
a file — see ``server/config.py``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI

    from novafabric.server.config import ServerConfig

#: Env var carrying the resolved server config path into worker processes.
CONFIG_ENV = "NOVAFABRIC_SERVER_CONFIG"

#: uvicorn import string for the ``--workers`` path (module:callable).
FACTORY_TARGET = "novafabric.server.factory:make_app"


def resolve_config(config_path: Path | None = None) -> "ServerConfig":
    """Load the server config, apply env overrides, and validate the bind.

    Shared by the single-worker CLI path and the multi-worker factory so both
    resolve the same effective config and enforce the same ADR-0184
    insecure-bind guard.
    """
    from novafabric.server.config import check_insecure_bind, load_config

    cfg = load_config(config_path)
    check_insecure_bind(cfg)
    return cfg


def make_app() -> "FastAPI":
    """uvicorn factory entrypoint used by ``nova server start --workers``.

    Each worker process calls this to build its own app from the config path
    in :data:`CONFIG_ENV` (falling back to the default location) plus the
    inherited ``NOVAFABRIC_SERVER_*`` overrides. In local (non-OIDC,
    non-insecure) mode it resolves the file-backed local token so every worker
    shares the same bearer token.
    """
    from novafabric.server.app import create_app
    from novafabric.server.request_id import configure_logging

    # Worker processes reconfigure logging from NOVAFABRIC_SERVER_LOG_FORMAT so
    # multi-worker output matches the single-worker path (text or json + rid).
    configure_logging()

    cfg_env = os.environ.get(CONFIG_ENV)
    cfg = resolve_config(Path(cfg_env) if cfg_env else None)

    if not cfg.oidc.enabled and not cfg.insecure_no_auth:
        from novafabric.server.local_token import ensure_local_token

        # File-backed + NOVAFABRIC_SERVER_TOKEN-pinnable, so all workers agree.
        token, _ = ensure_local_token()
        cfg.local_token = token

    return create_app(cfg)
