"""Shared pytest fixtures for lineage backend tests."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from novafabric.lineage._types import LineageEdge


def _sqlite_factory() -> object:
    """Return a fresh SqliteLineageStore backed by a temp file."""
    from novafabric.lineage.backends.sqlite import SqliteLineageStore

    tmp = Path(tempfile.mkdtemp()) / "lineage.db"
    return SqliteLineageStore(db_path=tmp)


# Register the SQLite backend with the conformance suite.
# This runs at conftest import time, which is after pythonpath is configured
# but before pytest_generate_tests evaluates REGISTERED_BACKENDS.
def _register_backends() -> None:
    from lineage.test_store_interface import REGISTERED_BACKENDS  # type: ignore[import]

    if _sqlite_factory not in REGISTERED_BACKENDS:
        REGISTERED_BACKENDS.append(_sqlite_factory)


_register_backends()


# Register KuzuDB backend if kuzu is installed.
try:
    import pathlib as _pathlib
    import tempfile as _tempfile

    import kuzu  # noqa: F401

    from novafabric.lineage.backends.kuzu import KuzuLineageStore

    def _kuzu_factory() -> object:
        """Return a fresh KuzuLineageStore backed by a temp directory."""
        return KuzuLineageStore(db_path=_pathlib.Path(_tempfile.mkdtemp()))

    from lineage.test_store_interface import REGISTERED_BACKENDS as _RB  # type: ignore[import]

    if _kuzu_factory not in _RB:
        _RB.append(_kuzu_factory)
except ImportError:
    pass  # kuzu not installed — conformance tests for KuzuDB skipped


def _run_node(run_id: str) -> dict[str, object]:
    """Build a run-kind node dict matching production _writer.py shape."""
    return {"kind": "run", "run_id": run_id}


@pytest.fixture
def sample_edges() -> list[LineageEdge]:
    """Return five LineageEdge instances forming a small DAG.

    Topology::

        A --contains--> B --spawned--> C --replayed_from--> D
        A --delegated_to--> C
        B --contains--> D
    """
    node_a = _run_node("run-A")
    node_b = _run_node("run-B")
    node_c = _run_node("run-C")
    node_d = _run_node("run-D")

    return [
        LineageEdge(
            edge_type="contains",
            source=node_a,
            target=node_b,
            confidence="high",
            capsule_run_id="run-001",
        ),
        LineageEdge(
            edge_type="spawned",
            source=node_b,
            target=node_c,
            confidence="high",
            capsule_run_id="run-001",
        ),
        LineageEdge(
            edge_type="delegated_to",
            source=node_a,
            target=node_c,
            confidence="medium",
            capsule_run_id="run-001",
        ),
        LineageEdge(
            edge_type="replayed_from",
            source=node_c,
            target=node_d,
            confidence="high",
            capsule_run_id="run-002",
        ),
        LineageEdge(
            edge_type="contains",
            source=node_b,
            target=node_d,
            confidence="high",
            capsule_run_id="run-002",
        ),
    ]
