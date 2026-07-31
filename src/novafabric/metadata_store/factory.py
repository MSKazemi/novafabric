"""Factory function for selecting the active MetadataStore backend.

Environment variables
---------------------
NOVAFABRIC_METADATA_BACKEND
    ``sqlite`` (default) or ``postgres``.
NOVAFABRIC_METADATA_DSN
    Required when backend is ``postgres``.  Must be a libpq-compatible DSN,
    e.g. ``postgresql://user:pass@host:5432/dbname``.
NOVAFABRIC_METADATA_DB_POOL
    ``1``/``true`` to back the postgres store with an opt-in psycopg connection
    pool (ADR-0221). Default off — unchanged per-request connect/close.
NOVAFABRIC_METADATA_DB_POOL_MIN / _MAX
    Pool sizing when the pool is enabled (defaults 1 / 10).
"""
from __future__ import annotations

import os

from novafabric.metadata_store.interface import MetadataStore


def _pool_enabled() -> bool:
    return os.environ.get("NOVAFABRIC_METADATA_DB_POOL", "").lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _pool_size(var: str, default: int) -> int:
    raw = os.environ.get(var)
    if not raw:
        return default
    try:
        return max(int(raw), 1)
    except ValueError:
        return default


def get_metadata_store() -> MetadataStore:
    """Return a MetadataStore instance configured from environment variables.

    Raises
    ------
    RuntimeError
        If ``NOVAFABRIC_METADATA_BACKEND=postgres`` but ``NOVAFABRIC_METADATA_DSN``
        is not set.
    """
    backend = os.environ.get("NOVAFABRIC_METADATA_BACKEND", "sqlite").lower()

    if backend == "postgres":
        dsn = os.environ.get("NOVAFABRIC_METADATA_DSN", "")
        if not dsn:
            raise RuntimeError(
                "NOVAFABRIC_METADATA_BACKEND=postgres requires NOVAFABRIC_METADATA_DSN "
                "to be set to a libpq-compatible DSN."
            )
        from novafabric.metadata_store.postgres import PostgresMetadataStore

        if _pool_enabled():
            return PostgresMetadataStore.with_pool(
                dsn,
                min_size=_pool_size("NOVAFABRIC_METADATA_DB_POOL_MIN", 1),
                max_size=_pool_size("NOVAFABRIC_METADATA_DB_POOL_MAX", 10),
            )
        return PostgresMetadataStore(dsn=dsn)

    from novafabric.metadata_store.sqlite import SQLiteMetadataStore

    return SQLiteMetadataStore()
