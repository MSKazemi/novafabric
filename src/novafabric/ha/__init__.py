"""High-availability primitives (ADR-0244).

Slice 1: the writer lease with fencing tokens — the data-model core that
makes split-brain a *rejected statement* rather than a race to detect. The
automated promotion loop, ``/readyz`` wiring, and write-path fencing checks
are later slices (see the ADR's Implementation status).
"""

from novafabric.ha.lease import (
    LeaseState,
    PostgresLeaseStore,
    SqliteLeaseStore,
    WriterLeaseStore,
)

__all__ = [
    "LeaseState",
    "PostgresLeaseStore",
    "SqliteLeaseStore",
    "WriterLeaseStore",
]
