"""Unit-level checks for PostgresLineageStore that need no live database.

The behavioural parity suite (against a real Postgres via testcontainers) lives
in ``test_postgres_backend.py``. These checks cover the parts that do not require
a connection: the class contract and the constructor's argument requirement.
"""

from __future__ import annotations

import inspect

from novafabric.lineage.backends.postgres import PostgresLineageStore
from novafabric.lineage.store import AbstractLineageStore


class TestPostgresLineageStoreContract:
    def test_is_abstract_lineage_store_subclass(self) -> None:
        assert issubclass(PostgresLineageStore, AbstractLineageStore)

    def test_constructor_requires_a_dsn(self) -> None:
        # The store is no longer a stub — it needs a Postgres DSN to connect.
        sig = inspect.signature(PostgresLineageStore.__init__)
        params = list(sig.parameters)
        assert "dsn" in params
        assert sig.parameters["dsn"].default is inspect.Parameter.empty

    def test_implements_all_abstract_methods(self) -> None:
        for name in ("insert", "provenance", "blast_radius", "replay_chain"):
            method = getattr(PostgresLineageStore, name)
            # Overridden concretely (not the abstract base method).
            assert method is not getattr(AbstractLineageStore, name)
