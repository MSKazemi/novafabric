"""Factory function for selecting the active MetadataStore backend.

Environment variables
---------------------
NOVAFABRIC_METADATA_BACKEND
    ``sqlite`` (default) or ``postgres``.
NOVAFABRIC_METADATA_DSN
    Required when backend is ``postgres``.  Must be a libpq-compatible DSN,
    e.g. ``postgresql://user:pass@host:5432/dbname``.
"""
from __future__ import annotations

import os

from novafabric.metadata_store.interface import MetadataStore


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

        return PostgresMetadataStore(dsn=dsn)

    from novafabric.metadata_store.sqlite import SQLiteMetadataStore

    return SQLiteMetadataStore()
