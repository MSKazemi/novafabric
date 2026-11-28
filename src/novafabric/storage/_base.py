from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass
class StorageInfo:
    backend: str
    db_path: str | None
    schema_version: int | None
    row_counts: dict[str, int] = field(default_factory=dict)
    migration_pending: bool | None = None  # None = check not available
    # SQLite only: False = the file does not exist yet (it would be created on first
    # use; the check does not create it). None = not applicable (postgres).
    db_exists: bool | None = None


@runtime_checkable
class StorageBackend(Protocol):
    def info(self) -> StorageInfo: ...
