# Lineage backend implementations.
# Each concrete backend lives in its own sub-module and subclasses
# :class:`novafabric.lineage.store.AbstractLineageStore`.

from novafabric.lineage.backends.age import AGELineageStore
from novafabric.lineage.backends.janusgraph import JanusGraphLineageStore
from novafabric.lineage.backends.postgres import PostgresLineageStore
from novafabric.lineage.backends.sqlite import SqliteLineageStore

# KuzuDB is an optional dependency — import only if kuzu is installed.
try:
    from novafabric.lineage.backends.kuzu import KuzuLineageStore

    _kuzu_available = True
except ImportError:
    _kuzu_available = False

__all__ = [
    "AGELineageStore",
    "JanusGraphLineageStore",
    "PostgresLineageStore",
    "SqliteLineageStore",
    "KuzuLineageStore",
]
