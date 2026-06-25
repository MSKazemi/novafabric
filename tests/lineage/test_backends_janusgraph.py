"""Tests for the JanusGraph lineage backend stub."""

from __future__ import annotations

import os

import pytest

from novafabric.lineage.backends.janusgraph import JanusGraphLineageStore
from novafabric.lineage.store import AbstractLineageStore

pytestmark = pytest.mark.skipif(
    os.environ.get("NOVA_INTEGRATION") != "1",
    reason="NOVA_INTEGRATION=1 not set — JanusGraph integration tests skipped",
)


def test_janusgraph_init_raises_import_or_not_implemented() -> None:
    """Instantiating JanusGraphLineageStore must raise ImportError or NotImplementedError."""
    with pytest.raises((ImportError, NotImplementedError)):
        JanusGraphLineageStore()


def test_janusgraph_is_subclass_of_abstract() -> None:
    """JanusGraphLineageStore must be a subclass of AbstractLineageStore."""
    assert issubclass(JanusGraphLineageStore, AbstractLineageStore)


def test_janusgraph_methods_exist() -> None:
    """JanusGraphLineageStore must define the four ABC methods."""
    assert hasattr(JanusGraphLineageStore, "insert")
    assert hasattr(JanusGraphLineageStore, "provenance")
    assert hasattr(JanusGraphLineageStore, "blast_radius")
    assert hasattr(JanusGraphLineageStore, "replay_chain")
