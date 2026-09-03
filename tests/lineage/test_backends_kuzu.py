"""Tests for the KuzuDB lineage backend."""

from __future__ import annotations

import pathlib

import pytest

kuzu = pytest.importorskip("kuzu")

from lineage import contract  # noqa: E402
from novafabric.lineage._types import LineageEdge, node_id_for  # noqa: E402
from novafabric.lineage.backends.kuzu import KuzuLineageStore  # noqa: E402
from novafabric.lineage.store import AbstractLineageStore  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_node(run_id: str) -> dict[str, object]:
    return {"kind": "run", "run_id": run_id}


def _make_edge(
    src: str,
    tgt: str,
    edge_type: str = "contains",
    capsule_run_id: str = "cap-001",
    site_id: str = "local",
) -> LineageEdge:
    return LineageEdge(
        edge_type=edge_type,
        source=_run_node(src),
        target=_run_node(tgt),
        confidence="high",
        capsule_run_id=capsule_run_id,
    )


def _make_store(tmp_path: pathlib.Path) -> KuzuLineageStore:
    return KuzuLineageStore(db_path=tmp_path / "lineage.kuzu")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_kuzu_insert_and_provenance(tmp_path: pathlib.Path) -> None:
    """Insert 5 edges, query provenance from root — must return descendants."""
    store = _make_store(tmp_path)
    edges = [
        _make_edge("run-A", "run-B"),
        _make_edge("run-B", "run-C"),
        _make_edge("run-A", "run-C", "delegated_to"),
        _make_edge("run-C", "run-D", "replayed_from", "cap-002"),
        _make_edge("run-B", "run-D", capsule_run_id="cap-002"),
    ]
    for e in edges:
        store.insert(e)

    result = store.provenance("run-A", depth=3)
    assert isinstance(result, list)
    assert {r["ref"] for r in result} == {"run-B", "run-C", "run-D"}
    assert {r["kind"] for r in result} == {"run"}


def test_kuzu_blast_radius(tmp_path: pathlib.Path) -> None:
    """Insert 5 edges, query blast_radius from leaf — must return ancestors."""
    store = _make_store(tmp_path)
    edges = [
        _make_edge("run-A", "run-B"),
        _make_edge("run-B", "run-C"),
        _make_edge("run-A", "run-C", "delegated_to"),
        _make_edge("run-C", "run-D", "replayed_from", "cap-002"),
        _make_edge("run-B", "run-D", capsule_run_id="cap-002"),
    ]
    for e in edges:
        store.insert(e)

    result = store.blast_radius("run-D", max_depth=5)
    assert isinstance(result, list)
    assert {r["ref"] for r in result} == {"run-A", "run-B", "run-C"}


def test_kuzu_replay_chain(tmp_path: pathlib.Path) -> None:
    """Insert edges with replayed_from type — replay_chain must return them."""
    store = _make_store(tmp_path)
    store.insert(_make_edge("run-C", "run-D", "replayed_from", "cap-002"))
    store.insert(_make_edge("run-A", "run-C"))  # non-replay edge

    result = store.replay_chain("run-C")
    assert [r["ref"] for r in result] == ["run-D"]


def test_kuzu_insert_idempotent(tmp_path: pathlib.Path) -> None:
    """Insert the same edge twice — no error, only one entry in results."""
    store = _make_store(tmp_path)
    edge = _make_edge("run-A", "run-B")
    store.insert(edge)
    store.insert(edge)  # must not raise

    result = store.provenance("run-A", depth=1)
    # Idempotent — run-B should appear exactly once.
    assert [r["ref"] for r in result].count("run-B") == 1


def test_kuzu_site_id_stored(tmp_path: pathlib.Path) -> None:
    """Insert edge with custom site_id — store holds it (provenance must work)."""
    store = KuzuLineageStore(db_path=tmp_path / "lineage.kuzu", site_id="cluster-eu-west-1")
    store.insert(_make_edge("run-X", "run-Y"))
    result = store.provenance("run-X", depth=1)
    assert len(result) == 1
    assert result[0]["ref"] == "run-Y"


def test_kuzu_tmp_dir_cleanup(tmp_path: pathlib.Path) -> None:
    """KuzuLineageStore with explicit db_path does not crash on use."""
    store = KuzuLineageStore(db_path=tmp_path / "lineage.kuzu")
    store.insert(_make_edge("run-A", "run-B"))
    assert store.provenance("run-A", depth=1) != []


def test_kuzu_empty_store(tmp_path: pathlib.Path) -> None:
    """All query methods on an empty store return empty lists."""
    store = _make_store(tmp_path)
    assert store.provenance("run-X", depth=3) == []
    assert store.blast_radius("run-X", max_depth=5) == []
    assert store.replay_chain("run-X") == []


def test_kuzu_depth_limit(tmp_path: pathlib.Path) -> None:
    """depth=1 returns fewer nodes than depth=3 on a 5-node chain."""
    store = _make_store(tmp_path)
    # Build a linear chain: A→B→C→D→E
    for src, tgt in [("A", "B"), ("B", "C"), ("C", "D"), ("D", "E")]:
        store.insert(_make_edge(f"run-{src}", f"run-{tgt}"))

    shallow = store.provenance("run-A", depth=1)
    deep = store.provenance("run-A", depth=3)
    assert len(shallow) < len(deep)


def test_kuzu_node_row_shape(tmp_path: pathlib.Path) -> None:
    """Every NodeRow carries node_id/kind/ref; replay rows also carry step.

    The replay arm used to be vacuous — it queried a run with no `replayed_from`
    edges, so the loop body never executed and the shape was never checked.
    """
    store = _make_store(tmp_path)
    store.insert(_make_edge("run-A", "run-B"))
    store.insert(_make_edge("run-B", "run-C", "replayed_from"))

    replay = store.replay_chain("run-B")
    assert replay, "replay arm must not be vacuous"

    for method_result in [
        store.provenance("run-A", depth=1),
        store.blast_radius("run-B", max_depth=1),
        replay,
    ]:
        for row in method_result:
            assert {"node_id", "kind", "ref"} <= set(row.keys())
            assert isinstance(row["node_id"], str)
            assert isinstance(row["kind"], str)
            assert isinstance(row["ref"], str)

    for row in replay:
        assert set(row.keys()) == {"node_id", "kind", "ref", "step"}
        assert isinstance(row["step"], int)


def test_kuzu_none_db_path_uses_temp(tmp_path: pathlib.Path) -> None:
    """`KuzuLineageStore(db_path=None)` must work without raising ValueError."""
    store = KuzuLineageStore(db_path=None)
    assert isinstance(store, AbstractLineageStore)
    store.insert(_make_edge("run-A", "run-B"))
    result = store.provenance("run-A", depth=1)
    assert len(result) == 1


def test_kuzu_is_abstract_lineage_store(tmp_path: pathlib.Path) -> None:
    """KuzuLineageStore must be a subclass of AbstractLineageStore."""
    store = _make_store(tmp_path)
    assert isinstance(store, AbstractLineageStore)


# ---------------------------------------------------------------------------
# The shared backend contract
# ---------------------------------------------------------------------------


class TestKuzuLineageContract:
    """Kuzu against the same contract SQLite and Postgres run.

    There are no exemptions. Both divergences this contract originally found —
    an asset stored as a run with an empty ref, and an unordered `replay_chain`
    — were fixed in ADR 0266 by moving Kuzu onto the same generic
    `node_id_for(kind, ref)` node model the other backends already use.
    """

    #: check name -> why Kuzu cannot pass it. Empty, and it must stay that way:
    #: an exemption here is a declared defect, not a configuration knob.
    DIVERGENCES: dict[str, object] = {}

    @pytest.fixture()
    def store(self, tmp_path: pathlib.Path) -> KuzuLineageStore:
        store = KuzuLineageStore(db_path=tmp_path / "contract.kuzu")
        contract.load(store)
        return store

    @pytest.mark.parametrize("check", contract.contract_params(DIVERGENCES))
    def test_contract(self, check: str, store: KuzuLineageStore) -> None:
        contract.CONTRACT_CHECKS[check](store)


# ---------------------------------------------------------------------------
# Regressions for the two divergences fixed in ADR 0266
# ---------------------------------------------------------------------------


def test_kuzu_node_id_is_the_canonical_cross_backend_identity(
    tmp_path: pathlib.Path,
) -> None:
    """`node_id` is `node_id_for(kind, ref)`, as in SQLite/Postgres/AGE.

    Kuzu used to key nodes on the raw `run_id`, so the same logical node had a
    different `node_id` here than in every other backend.
    """
    store = _make_store(tmp_path)
    store.insert(_make_edge("run-A", "run-B"))

    for row in store.provenance("run-A", depth=1):
        assert row["node_id"] == node_id_for(row["kind"], row["ref"])


def test_kuzu_provenance_crosses_the_run_to_asset_edge(
    tmp_path: pathlib.Path,
) -> None:
    """An asset is a first-class node, not a Run with an empty run_id.

    This is divergence (a): the legacy schema had only a `Run` table, so the
    asset endpoint was stored with an empty ref and reported as `kind="run"`.
    """
    store = _make_store(tmp_path)
    store.insert(
        LineageEdge(
            edge_type="consumed",
            source={"kind": "run", "run_id": "run-A"},
            target={"kind": "asset", "asset_ref": "model:foo@1.0.0", "registry": "local"},
            confidence="high",
            capsule_run_id="run-A",
        )
    )

    rows = store.provenance("run-A", depth=5)
    assert len(rows) == 1
    assert rows[0]["kind"] == "asset"
    assert rows[0]["ref"] == "local:model:foo@1.0.0"


def test_kuzu_replay_chain_order_is_deterministic(tmp_path: pathlib.Path) -> None:
    """Divergence (b): the order must be stable across fresh stores.

    The original defect was *non-determinism*, not a stable wrong answer —
    measured at 5 distinct orderings over 40 fresh stores on identical input,
    correct only 3/40. A single green run therefore proves nothing, so this
    rebuilds the store 40 times and requires all 40 to agree.
    """
    orderings = set()
    for i in range(40):
        store = KuzuLineageStore(db_path=tmp_path / f"det-{i}.kuzu")
        for src, tgt in [("run-B", "run-A"), ("run-C", "run-B"), ("run-D", "run-C")]:
            store.insert(_make_edge(src, tgt, "replayed_from"))
        orderings.add(tuple(r["ref"] for r in store.replay_chain("run-D")))

    assert orderings == {("run-C", "run-B", "run-A")}


def test_kuzu_replay_chain_steps_are_hop_distances(tmp_path: pathlib.Path) -> None:
    """`step` is the hop distance from the anchor, nearest ancestor first."""
    store = _make_store(tmp_path)
    for src, tgt in [("run-B", "run-A"), ("run-C", "run-B"), ("run-D", "run-C")]:
        store.insert(_make_edge(src, tgt, "replayed_from"))

    assert [(r["ref"], r["step"]) for r in store.replay_chain("run-D")] == [
        ("run-C", 1),
        ("run-B", 2),
        ("run-A", 3),
    ]


def test_kuzu_replay_chain_never_reports_a_run_as_its_own_ancestor(
    tmp_path: pathlib.Path,
) -> None:
    """A cyclic (corrupt) graph must not put the anchor in its own chain."""
    store = _make_store(tmp_path)
    for src, tgt in [("run-A", "run-B"), ("run-B", "run-C"), ("run-C", "run-A")]:
        store.insert(_make_edge(src, tgt, "replayed_from"))

    refs = [r["ref"] for r in store.replay_chain("run-A")]
    assert "run-A" not in refs
    assert refs == ["run-B", "run-C"]


def test_kuzu_legacy_schema_is_migrated_not_orphaned(
    tmp_path: pathlib.Path,
) -> None:
    """A pre-0.102 `Run`/`LINEAGE` database is carried into the new model.

    Opening such a database must not silently present it as empty. The one thing
    that genuinely cannot be recovered is an edge whose non-run endpoint was
    stored as a `Run` with an empty `run_id` — the legacy schema never persisted
    its ref.
    """
    db_path = tmp_path / "legacy.kuzu"
    conn = kuzu.Connection(kuzu.Database(str(db_path)))
    conn.execute("CREATE NODE TABLE Run(run_id STRING, PRIMARY KEY(run_id))")
    conn.execute(
        "CREATE REL TABLE LINEAGE(FROM Run TO Run, edge_id STRING, "
        "edge_type STRING, depth INT32, signature_ref STRING, "
        "capsule_run_id STRING, site_id STRING)"
    )
    for run_id in ["run-A", "run-B", ""]:
        conn.execute("MERGE (r:Run {run_id: $r})", {"r": run_id})
    for src, tgt, eid, etype in [
        ("run-B", "run-A", "e1", "replayed_from"),
        ("run-A", "", "e0", "consumed"),  # the unrecoverable asset edge
    ]:
        conn.execute(
            "MATCH (a:Run {run_id: $s}), (b:Run {run_id: $t}) "
            "CREATE (a)-[:LINEAGE {edge_id: $e, edge_type: $et, depth: 1, "
            "signature_ref: '', capsule_run_id: $s, site_id: 'local'}]->(b)",
            {"s": src, "t": tgt, "e": eid, "et": etype},
        )
    del conn

    store = KuzuLineageStore(db_path=db_path)

    # The run-to-run edge survived, under the new canonical identity.
    chain = store.replay_chain("run-B")
    assert [r["ref"] for r in chain] == ["run-A"]
    assert chain[0]["node_id"] == node_id_for("run", "run-A")

    # The legacy tables are gone, so the migration cannot run twice.
    assert store._table_names() == {"Node", "LEDGE"}

    # Reopening a migrated database is a no-op that preserves the data.
    reopened = KuzuLineageStore(db_path=db_path)
    assert [r["ref"] for r in reopened.replay_chain("run-B")] == ["run-A"]
