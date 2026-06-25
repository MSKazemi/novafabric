from __future__ import annotations

import os
from pathlib import Path

from novafabric.storage._azure_worm import AzureWormAdapter
from novafabric.storage._base import StorageBackend, StorageInfo
from novafabric.storage._gcs_worm import GCSWormAdapter
from novafabric.storage._local_worm import LocalWormAdapter
from novafabric.storage._retention import LegalHold, RetentionPolicy, RetentionPolicyEngine
from novafabric.storage._s3_worm import S3WormAdapter
from novafabric.storage._sqlite import SQLiteBackend
from novafabric.storage.worm import IntegrityResult, WormAdapter, WormEntry, WormReceipt

__all__ = [
    "StorageBackend",
    "StorageInfo",
    "SQLiteBackend",
    "get_backend",
    "WormAdapter",
    "WormReceipt",
    "WormEntry",
    "IntegrityResult",
    "LocalWormAdapter",
    "S3WormAdapter",
    "AzureWormAdapter",
    "GCSWormAdapter",
    "LegalHold",
    "RetentionPolicy",
    "RetentionPolicyEngine",
]


def get_backend(
    backend: str | None = None,
    db_path: Path | None = None,
    postgres_dsn: str | None = None,
) -> StorageBackend:
    """Return a StorageBackend for the given configuration.

    Resolution order:
      1. ``backend`` argument
      2. ``NOVAFABRIC_BACKEND`` env var (``sqlite`` or ``postgres``)
      3. Default: ``sqlite``
    """
    resolved = backend or os.environ.get("NOVAFABRIC_BACKEND", "sqlite")

    if resolved == "postgres":
        dsn = postgres_dsn or os.environ.get("NOVAFABRIC_POSTGRES_DSN", "")
        if not dsn:
            raise ValueError(
                "Postgres backend selected but no DSN provided. "
                "Set NOVAFABRIC_POSTGRES_DSN or pass --postgres-dsn."
            )
        from novafabric.storage._postgres import PostgresBackend

        return PostgresBackend(dsn)

    if resolved == "sqlite":
        return SQLiteBackend(db_path)

    raise ValueError(f"Unknown backend: {resolved!r}. Choose 'sqlite' or 'postgres'.")
