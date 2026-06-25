"""FastAPI dependency providers for the NovaFabric multi-user server."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated

from fastapi import Depends, Request

from novafabric._paths import default_capsule_dir
from novafabric.metadata_store import MetadataStore as _MetadataStore
from novafabric.metadata_store import get_metadata_store
from novafabric.server.config import ServerConfig


def get_config(request: Request) -> ServerConfig:
    """Retrieve the ServerConfig attached to the app state."""
    cfg: ServerConfig = request.app.state.config
    return cfg


def get_db_path(
    config: Annotated[ServerConfig, Depends(get_config)],
) -> Path | None:
    """Return the SQLite db path (or None for default) from config."""
    if config.db_path:
        return Path(config.db_path)
    return None


def get_capsule_dir(
    config: Annotated[ServerConfig, Depends(get_config)],
) -> Path:
    """Return the capsule storage directory from config, creating it if absent."""
    capsule_dir = default_capsule_dir()
    capsule_dir.mkdir(parents=True, exist_ok=True)
    return capsule_dir


@lru_cache(maxsize=1)
def _cached_metadata_store() -> _MetadataStore:
    store = get_metadata_store()
    store.bootstrap()
    return store


def get_metadata_store_dep() -> _MetadataStore:
    """FastAPI dependency: returns the configured MetadataStore singleton."""
    return _cached_metadata_store()
