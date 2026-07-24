"""Unit-level checks for AGELineageStore that need no live database.

The behavioural parity suite (against a real Apache AGE via testcontainers) lives
in ``test_age_backend.py``. These checks cover the class contract only — the
constructor now connects to a live AGE-enabled Postgres and is exercised there.
"""

from __future__ import annotations

import inspect

from novafabric.lineage.backends.age import AGELineageStore
from novafabric.lineage.store import AbstractLineageStore


def test_age_is_subclass_of_abstract() -> None:
    assert issubclass(AGELineageStore, AbstractLineageStore)


def test_age_constructor_requires_a_dsn() -> None:
    # No longer a stub — it needs a DSN to an AGE-enabled Postgres to connect.
    sig = inspect.signature(AGELineageStore.__init__)
    assert "dsn" in sig.parameters
    assert sig.parameters["dsn"].default is inspect.Parameter.empty


def test_age_implements_all_abstract_methods() -> None:
    for name in ("insert", "provenance", "blast_radius", "replay_chain"):
        method = getattr(AGELineageStore, name)
        assert method is not getattr(AbstractLineageStore, name)
