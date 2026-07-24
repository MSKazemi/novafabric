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


def _seed_edge(
    i: int,
    edge_type: str,
    source: dict[str, object],
    target: dict[str, object],
    confidence: str = "observed",
    facets: dict[str, object] | None = None,
) -> LineageEdge:
    """Deterministic edge for the seeded graph: fixed edge_id and created_at."""
    return LineageEdge(
        edge_type=edge_type,
        source=source,
        target=target,
        confidence=confidence,
        capsule_run_id="seed-cap",
        edge_id=f"e{i:03d}",
        created_at=f"2026-07-01T00:00:{i:02d}.000000Z",
        facets=facets,
    )


@pytest.fixture
def seeded_lineage_store(tmp_path: Path) -> object:
    """A ``LineageStore`` over a small deterministic graph (ADR-0212..0215 tests).

    Topology (source --type--> target); every id/timestamp is fixed so exports
    are byte-stable::

        run-1..run-4 --consumed--> HUB(local:hub-model@v1)     # degree hub
        run-1 --delegated_to--> run-bridge --spawned--> run-c1  # bridge =
        run-c1 --contains--> run-c2 --contains--> run-c3        # articulation
        run-c3 --spawned--> run-cy1 <--delegated_to--> run-cy2  # 2-cycle
        run-victim  --consumed--> stale-data@v3 --produced_by--> run-bad (failed)
        run-victim2 --consumed--> stale-data@v3                 # correlation
        run-victim  --consumed--> HUB                           # joins components
        orphan-dataset (external)                               # no edges
    """
    from novafabric.lineage._store import LineageStore
    from novafabric.lineage._types import LineageNode, node_id_for

    store = LineageStore(db_path=tmp_path / "lineage-seeded.db")

    hub = {"kind": "asset", "registry": "local", "asset_ref": "hub-model@v1"}
    asset_x = {"kind": "asset", "registry": "local", "asset_ref": "stale-data@v3"}
    runs = {
        name: _run_node(name)
        for name in (
            "run-1", "run-2", "run-3", "run-4", "run-bridge",
            "run-c1", "run-c2", "run-c3", "run-cy1", "run-cy2",
        )
    }
    victim = {
        "kind": "run", "run_id": "run-victim",
        "status": "failed", "error": "consumed stale upstream data",
    }
    victim2 = {
        "kind": "run", "run_id": "run-victim2",
        "status": "failed", "error": "bad input",
    }
    bad = {
        "kind": "run", "run_id": "run-bad",
        "status": "failed", "error": "tool call timeout",
        "finished_at": "2026-07-03T00:00:00.000000Z",
    }

    edges = [
        _seed_edge(1, "consumed", runs["run-1"], hub),
        _seed_edge(2, "consumed", runs["run-2"], hub),
        _seed_edge(3, "consumed", runs["run-3"], hub),
        _seed_edge(4, "consumed", runs["run-4"], hub),
        _seed_edge(5, "delegated_to", runs["run-1"], runs["run-bridge"]),
        _seed_edge(6, "spawned", runs["run-bridge"], runs["run-c1"]),
        _seed_edge(7, "contains", runs["run-c1"], runs["run-c2"]),
        _seed_edge(8, "contains", runs["run-c2"], runs["run-c3"]),
        _seed_edge(9, "spawned", runs["run-c3"], runs["run-cy1"]),
        _seed_edge(10, "delegated_to", runs["run-cy1"], runs["run-cy2"]),
        _seed_edge(11, "delegated_to", runs["run-cy2"], runs["run-cy1"]),
        _seed_edge(
            12, "consumed", victim, asset_x,
            confidence="inferred", facets={"demo": {"note": "seeded facet"}},
        ),
        _seed_edge(13, "produced_by", asset_x, bad),
        _seed_edge(14, "consumed", victim2, asset_x),
        _seed_edge(15, "consumed", victim, hub),
    ]
    for edge in edges:
        store.insert_edge(edge)

    orphan_ref = "orphan-dataset"
    store.replace_capsule_lineage(
        nodes=[
            LineageNode(
                node_id=node_id_for("external", orphan_ref),
                kind="external",
                ref=orphan_ref,
                first_seen_capsule_run_id=None,
                payload={"kind": "external", "ref": orphan_ref},
            )
        ],
        edges=[],
        capsule_run_id="seed-orphan",
    )
    return store


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
