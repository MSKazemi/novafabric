"""Tests for the Apache AGE lineage backend stub."""

from __future__ import annotations

import pytest

from novafabric.lineage.backends.age import AGELineageStore
from novafabric.lineage.store import AbstractLineageStore


def test_age_init_raises_not_implemented() -> None:
    """Instantiating AGELineageStore must raise NotImplementedError."""
    with pytest.raises(NotImplementedError, match="AGELineageStore requires"):
        AGELineageStore("postgresql://localhost/test")


def test_age_methods_exist() -> None:
    """AGELineageStore must define the four ABC methods."""
    assert hasattr(AGELineageStore, "insert")
    assert hasattr(AGELineageStore, "provenance")
    assert hasattr(AGELineageStore, "blast_radius")
    assert hasattr(AGELineageStore, "replay_chain")


def test_age_is_subclass_of_abstract() -> None:
    """AGELineageStore must be a subclass of AbstractLineageStore."""
    assert issubclass(AGELineageStore, AbstractLineageStore)
