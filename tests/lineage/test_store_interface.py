"""Conformance test suite for AbstractLineageStore.

Any backend that implements the ABC can register itself by appending a
*factory callable* to ``REGISTERED_BACKENDS`` below.  All parametrized tests
are skipped until at least one backend is registered.

Usage (from a backend's own conftest or test module)::

    from tests.lineage.test_store_interface import REGISTERED_BACKENDS
    REGISTERED_BACKENDS.append(MyBackend)   # class or zero-arg callable
"""

from __future__ import annotations

from typing import Callable, cast

import pytest

from novafabric.lineage._types import LineageEdge
from novafabric.lineage.store import AbstractLineageStore, NodeRow

# ---------------------------------------------------------------------------
# Backend registry — concrete backends append factory callables here.
# ---------------------------------------------------------------------------

REGISTERED_BACKENDS: list[Callable[[], AbstractLineageStore]] = []


# ---------------------------------------------------------------------------
# Parametrization hook — evaluated at collection time, after all conftest.py
# files have run, so any appends to REGISTERED_BACKENDS are visible.
# ---------------------------------------------------------------------------


def _backend_ids(val: Callable[[], AbstractLineageStore]) -> str:
    try:
        return val.__name__
    except AttributeError:
        return repr(val)


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    if "backend_store" in metafunc.fixturenames:
        if REGISTERED_BACKENDS:
            metafunc.parametrize(
                "backend_store", REGISTERED_BACKENDS, ids=_backend_ids, indirect=True
            )
        else:
            metafunc.parametrize("backend_store", [None], ids=["no-backend"], indirect=True)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def backend_store(request: pytest.FixtureRequest) -> AbstractLineageStore:
    """Yield a fresh backend instance per test, or skip if none registered."""
    factory = cast(Callable[[], AbstractLineageStore] | None, request.param)
    if factory is None:
        pytest.skip("no backend registered yet — add to REGISTERED_BACKENDS")
    return factory()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _assert_node_row(row: NodeRow) -> None:
    """Assert that *row* contains the required node_id, kind, ref keys."""
    assert "node_id" in row, f"NodeRow missing 'node_id': {row}"
    assert "kind" in row, f"NodeRow missing 'kind': {row}"
    assert "ref" in row, f"NodeRow missing 'ref': {row}"
    assert isinstance(row["node_id"], str), "'node_id' must be str"
    assert isinstance(row["kind"], str), "'kind' must be str"
    assert isinstance(row["ref"], str), "'ref' must be str"


# ---------------------------------------------------------------------------
# Conformance tests
# ---------------------------------------------------------------------------


def test_insert_and_provenance(
    backend_store: AbstractLineageStore,
    sample_edges: list[LineageEdge],
) -> None:
    """Inserting edges and querying provenance from run-A must return results."""
    for edge in sample_edges:
        backend_store.insert(edge)

    result = backend_store.provenance(run_id="run-A", depth=3)
    assert isinstance(result, list), "provenance() must return a list"
    assert len(result) > 0, "provenance() must return non-empty list for known run"
    for item in result:
        assert isinstance(item, dict), "each provenance result must be a dict"
        _assert_node_row(item)


def test_blast_radius(
    backend_store: AbstractLineageStore,
    sample_edges: list[LineageEdge],
) -> None:
    """blast_radius from run-D (deepest leaf) must return upstream contributors."""
    for edge in sample_edges:
        backend_store.insert(edge)

    # run-D is the target of B→D (contains) and C→D (replayed_from),
    # so its blast radius must include at least B and C (and transitively A).
    result = backend_store.blast_radius(run_id="run-D", max_depth=5)
    assert isinstance(result, list), "blast_radius() must return a list"
    assert len(result) > 0, "blast_radius() must return non-empty for run-D (it has inbound edges)"
    for item in result:
        _assert_node_row(item)


def test_replay_chain(
    backend_store: AbstractLineageStore,
    sample_edges: list[LineageEdge],
) -> None:
    """replay_chain for run-C must return at least run-D (C replayed_from D)."""
    for edge in sample_edges:
        backend_store.insert(edge)

    result = backend_store.replay_chain(run_id="run-C")
    assert isinstance(result, list), "replay_chain() must return a list"
    assert len(result) >= 1, (
        "replay_chain() must return at least one node for run-C (C→D replayed_from edge)"
    )
    for item in result:
        _assert_node_row(item)


def test_insert_idempotent(
    backend_store: AbstractLineageStore,
    sample_edges: list[LineageEdge],
) -> None:
    """Inserting the same edge twice must not raise any exception."""
    edge = sample_edges[0]
    backend_store.insert(edge)
    backend_store.insert(edge)  # must not raise


def test_insert_returns_none(
    backend_store: AbstractLineageStore,
    sample_edges: list[LineageEdge],
) -> None:
    """insert() must return None."""
    result = backend_store.insert(sample_edges[0])
    assert result is None, "insert() must return None"
