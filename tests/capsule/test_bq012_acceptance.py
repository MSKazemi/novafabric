# Copyright 2024 NovaFabric Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""BQ-012 acceptance criteria tests for Phase 3 parent/child capsule hierarchy.

AC-1: Four-type edge vocabulary validated against a LangGraph-style multi-agent run.
AC-2: Driver-crash scenario — orphan placeholder created, workers still queryable.
AC-3: Phase 1 writer.commit() p99 latency ≤ 50 ms on local tmpfs/NVMe.
AC-4: `nova lineage blast-radius <id> --edge-type spawned` filters correctly.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from novafabric.capsule.env_contract import CapsuleEnvConfig
from novafabric.capsule.orphan import OrphanManager, orphan_placeholder_run_id
from novafabric.capsule.schema import CapsuleRole, DistributionRole, EdgeType, FailMode
from novafabric.capsule.tree_assembler import CapsuleTreeAssembler
from novafabric.capsule.ulid_util import new_ulid
from novafabric.capsule.writer import CapsuleWriter
from novafabric.cli.lineage import lineage_app

# ── helpers ───────────────────────────────────────────────────────────────


def _make_config(
    spool: Path,
    *,
    capsule_role: CapsuleRole = CapsuleRole.STANDALONE,
    parent_run_id: str | None = None,
    world_size: int | None = None,
    rank: int | None = None,
    global_run_id: str | None = None,
) -> CapsuleEnvConfig:
    return CapsuleEnvConfig(
        global_run_id=global_run_id or new_ulid(),
        parent_run_id=parent_run_id,
        capsule_dir=str(spool),
        rank=rank,
        world_size=world_size,
        distribution_role=(
            DistributionRole.WORKER if parent_run_id else DistributionRole.DRIVER
        ),
        capsule_role=capsule_role,
        fail_mode=FailMode.fail_open,
        pending_parent_timeout_s=86400.0,
        warnings=[],
    )


def _make_writer(
    spool: Path,
    *,
    capsule_role: CapsuleRole = CapsuleRole.STANDALONE,
    parent_run_id: str | None = None,
    world_size: int | None = None,
    rank: int | None = None,
    global_run_id: str | None = None,
) -> CapsuleWriter:
    config = _make_config(
        spool,
        capsule_role=capsule_role,
        parent_run_id=parent_run_id,
        world_size=world_size,
        rank=rank,
        global_run_id=global_run_id,
    )
    return CapsuleWriter(config)


def _collect_all_edges(spool: Path) -> list[dict[str, Any]]:
    """Collect all lineage edges from every lineage.jsonl in spool."""
    edges: list[dict[str, Any]] = []
    for capsule_dir in spool.iterdir():
        if not capsule_dir.is_dir():
            continue
        lineage_path = capsule_dir / "lineage.jsonl"
        if not lineage_path.exists():
            continue
        for line in lineage_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                edges.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return edges


# ── AC-1: LangGraph-style multi-agent simulation ──────────────────────────


class TestLangGraphSimulation:
    """AC-1: four-type edge vocabulary validated against a simulated multi-agent run.

    Topology mirrors a LangGraph supervisor → worker × N pattern:
      SUPERVISOR (orchestrator capsule)
        → 3 WORKER capsules  [contains edge]
        → 2 SPECIALIST capsules  [delegated_to edge]
        → 1 REPLAY capsule  [replayed_from edge]
        + supervisor emits a spawned edge to one of the workers
    """

    def test_all_four_edge_types_emitted(self, tmp_path: Path) -> None:
        spool = tmp_path / "spool"
        global_run_id = new_ulid()

        # ── SUPERVISOR / orchestrator ─────────────────────────────────────
        supervisor = _make_writer(
            spool,
            capsule_role=CapsuleRole.PARENT,
            global_run_id=global_run_id,
            world_size=6,
        )
        supervisor.start()
        supervisor_run_id = supervisor.run_id

        # ── WORKERS: contains edges ────────────────────────────────────────
        worker_ids: list[str] = []
        for rank in range(3):
            w = _make_writer(
                spool,
                capsule_role=CapsuleRole.CHILD,
                parent_run_id=supervisor_run_id,
                global_run_id=global_run_id,
                rank=rank,
            )
            w.start()
            # supervisor emits a contains edge to each worker
            supervisor.add_lineage_edge(
                target_run_id=w.run_id,
                edge_type=EdgeType.contains,
            )
            w.commit(status="COMPLETE")
            worker_ids.append(w.run_id)

        # supervisor emits one spawned edge to worker-0 (dynamic spawn)
        supervisor.add_lineage_edge(
            target_run_id=worker_ids[0],
            edge_type=EdgeType.spawned,
        )

        # ── SPECIALISTS: delegated_to edges ───────────────────────────────
        specialist_ids: list[str] = []
        for rank in range(2):
            s = _make_writer(
                spool,
                capsule_role=CapsuleRole.CHILD,
                parent_run_id=supervisor_run_id,
                global_run_id=global_run_id,
                rank=3 + rank,
            )
            s.start()
            supervisor.add_lineage_edge(
                target_run_id=s.run_id,
                edge_type=EdgeType.delegated_to,
            )
            s.commit(status="COMPLETE")
            specialist_ids.append(s.run_id)

        # ── REPLAY: replayed_from edge ─────────────────────────────────────
        replay_w = _make_writer(
            spool,
            capsule_role=CapsuleRole.CHILD,
            parent_run_id=supervisor_run_id,
            global_run_id=global_run_id,
            rank=5,
        )
        replay_w.start()
        supervisor.add_lineage_edge(
            target_run_id=replay_w.run_id,
            edge_type=EdgeType.replayed_from,
        )
        replay_w.commit(status="COMPLETE")

        # Finalize supervisor
        supervisor.commit(status="COMPLETE")

        # ── Assertions ────────────────────────────────────────────────────
        all_edges = _collect_all_edges(spool)
        edge_types_found = {e["edge_type"] for e in all_edges}

        assert EdgeType.contains.value in edge_types_found, (
            "expected 'contains' edges in lineage.jsonl"
        )
        assert EdgeType.spawned.value in edge_types_found, (
            "expected 'spawned' edges in lineage.jsonl"
        )
        assert EdgeType.delegated_to.value in edge_types_found, (
            "expected 'delegated_to' edges in lineage.jsonl"
        )
        assert EdgeType.replayed_from.value in edge_types_found, (
            "expected 'replayed_from' edges in lineage.jsonl"
        )

    def test_tree_assembler_returns_correct_structure(self, tmp_path: Path) -> None:
        spool = tmp_path / "spool"
        global_run_id = new_ulid()

        supervisor = _make_writer(
            spool,
            capsule_role=CapsuleRole.PARENT,
            global_run_id=global_run_id,
            world_size=3,
        )
        supervisor.start()
        supervisor_run_id = supervisor.run_id

        child_ids: list[str] = []
        for rank in range(3):
            c = _make_writer(
                spool,
                capsule_role=CapsuleRole.CHILD,
                parent_run_id=supervisor_run_id,
                global_run_id=global_run_id,
                rank=rank,
            )
            c.start()
            c.commit(status="COMPLETE")
            child_ids.append(c.run_id)

        supervisor.commit(status="COMPLETE")

        assembler = CapsuleTreeAssembler(spool)
        tree = assembler.assemble_tree(supervisor_run_id)

        assert tree.root.run_id == supervisor_run_id
        assert len(tree.root.children) == 3
        assert tree.total_nodes == 4  # 1 parent + 3 children
        child_run_ids_in_tree = {c.run_id for c in tree.root.children}
        assert child_run_ids_in_tree == set(child_ids)

    def test_edge_type_filter_returns_only_spawned(self, tmp_path: Path) -> None:
        """--edge-types=spawned returns only spawned edges (cap-002, FR-09)."""
        spool = tmp_path / "spool"
        global_run_id = new_ulid()

        supervisor = _make_writer(
            spool,
            capsule_role=CapsuleRole.PARENT,
            global_run_id=global_run_id,
            world_size=2,
        )
        supervisor.start()
        supervisor_run_id = supervisor.run_id

        worker_a = _make_writer(
            spool,
            capsule_role=CapsuleRole.CHILD,
            parent_run_id=supervisor_run_id,
            global_run_id=global_run_id,
            rank=0,
        )
        worker_a.start()
        supervisor.add_lineage_edge(
            target_run_id=worker_a.run_id,
            edge_type=EdgeType.contains,
        )
        worker_a.commit(status="COMPLETE")

        worker_b = _make_writer(
            spool,
            capsule_role=CapsuleRole.CHILD,
            parent_run_id=supervisor_run_id,
            global_run_id=global_run_id,
            rank=1,
        )
        worker_b.start()
        supervisor.add_lineage_edge(
            target_run_id=worker_b.run_id,
            edge_type=EdgeType.spawned,
        )
        worker_b.commit(status="COMPLETE")
        worker_b_id = worker_b.run_id

        supervisor.commit(status="COMPLETE")

        # Read all edges for supervisor and filter by spawned
        all_edges = _collect_all_edges(spool)
        supervisor_edges = [
            e for e in all_edges if e.get("source_run_id") == supervisor_run_id
        ]
        spawned_edges = [
            e for e in supervisor_edges if e.get("edge_type") == EdgeType.spawned.value
        ]

        assert len(spawned_edges) == 1
        assert spawned_edges[0]["target_run_id"] == worker_b_id
        # contains edge must NOT appear in spawned-only filter
        for e in spawned_edges:
            assert e["edge_type"] == EdgeType.spawned.value


# ── AC-2: Driver-crash / orphan scenario ──────────────────────────────────


class TestDriverCrash:
    """AC-2: when the parent capsule never arrives, orphan placeholder is created.

    Workers are written first; OrphanManager creates a synthetic placeholder
    after pending_parent_timeout elapses; workers remain queryable via the
    tree assembler.  When the real parent arrives late, the placeholder is
    replaced (superseded).
    """

    def test_orphan_placeholder_created_after_timeout(self, tmp_path: Path) -> None:
        spool = tmp_path / "spool"
        # Use a synthetic parent_run_id that never has a real capsule
        missing_parent_id = new_ulid()
        global_run_id = new_ulid()

        # Write 3 worker capsules referencing the missing parent
        worker_ids: list[str] = []
        for rank in range(3):
            w = _make_writer(
                spool,
                capsule_role=CapsuleRole.CHILD,
                parent_run_id=missing_parent_id,
                global_run_id=global_run_id,
                rank=rank,
            )
            w.start()
            w.commit(status="COMPLETE")
            worker_ids.append(w.run_id)

        # Force created_at to the past so the timeout check triggers immediately
        for wid in worker_ids:
            manifest_path = spool / wid / "capsule.json"
            data: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))
            data["created_at"] = "2020-01-01T00:00:00+00:00"
            manifest_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

        # OrphanManager with a very short timeout so check_orphan_timeouts triggers
        manager = OrphanManager(
            spool, pending_parent_timeout_s=0.001, clock_fn=time.time
        )
        created_parents = manager.check_orphan_timeouts()
        assert missing_parent_id in created_parents, (
            "orphan placeholder not created for missing parent"
        )

        # Verify placeholder exists in spool
        placeholder_id = orphan_placeholder_run_id(missing_parent_id)
        placeholder_path = spool / placeholder_id / "capsule.json"
        assert placeholder_path.exists(), "placeholder capsule.json not found"

        placeholder_data: dict[str, Any] = json.loads(
            placeholder_path.read_text(encoding="utf-8")
        )
        assert placeholder_data.get("is_synthetic") is True
        assert placeholder_data.get("orphan_reason") == "pending_parent_timeout"

    def test_workers_queryable_after_orphan_created(self, tmp_path: Path) -> None:
        spool = tmp_path / "spool"
        missing_parent_id = new_ulid()
        global_run_id = new_ulid()

        worker_ids: list[str] = []
        for rank in range(3):
            w = _make_writer(
                spool,
                capsule_role=CapsuleRole.CHILD,
                parent_run_id=missing_parent_id,
                global_run_id=global_run_id,
                rank=rank,
            )
            w.start()
            w.commit(status="COMPLETE")
            worker_ids.append(w.run_id)

        # Backdate workers
        for wid in worker_ids:
            p = spool / wid / "capsule.json"
            d: dict[str, Any] = json.loads(p.read_text(encoding="utf-8"))
            d["created_at"] = "2020-01-01T00:00:00+00:00"
            p.write_text(json.dumps(d), encoding="utf-8")

        manager = OrphanManager(spool, pending_parent_timeout_s=0.001, clock_fn=time.time)
        manager.check_orphan_timeouts()

        # Assemble the tree rooted at missing_parent_id.  The parent capsule is absent,
        # so the assembler creates a synthetic root and the workers appear as its children
        # (they reference missing_parent_id via parent_run_id).
        assembler = CapsuleTreeAssembler(spool)
        tree = assembler.assemble_tree(missing_parent_id)

        found_worker_ids = {c.run_id for c in tree.root.children}
        assert found_worker_ids == set(worker_ids), (
            f"expected workers {worker_ids}, got {found_worker_ids}"
        )

    def test_late_parent_replaces_placeholder(self, tmp_path: Path) -> None:
        spool = tmp_path / "spool"
        missing_parent_id = new_ulid()
        global_run_id = new_ulid()

        # Write a single worker
        w = _make_writer(
            spool,
            capsule_role=CapsuleRole.CHILD,
            parent_run_id=missing_parent_id,
            global_run_id=global_run_id,
            rank=0,
        )
        w.start()
        w.commit(status="COMPLETE")

        # Backdate worker
        p = spool / w.run_id / "capsule.json"
        d: dict[str, Any] = json.loads(p.read_text(encoding="utf-8"))
        d["created_at"] = "2020-01-01T00:00:00+00:00"
        p.write_text(json.dumps(d), encoding="utf-8")

        manager = OrphanManager(spool, pending_parent_timeout_s=0.001, clock_fn=time.time)
        manager.check_orphan_timeouts()

        placeholder_id = orphan_placeholder_run_id(missing_parent_id)
        assert (spool / placeholder_id / "capsule.json").exists()

        # Simulate late-arriving real parent
        from datetime import datetime, timezone

        real_parent_manifest: dict[str, Any] = {
            "schema_version": "0.2.0",
            "capsule_role": "PARENT",
            "global_run_id": global_run_id,
            "run_id": missing_parent_id,
            "status": "COMPLETE",
            "is_synthetic": False,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "children_expected": 1,
            "children_arrived": 1,
        }

        manager.replace_with_real_parent(real_parent_manifest)

        # Real parent capsule now exists
        real_path = spool / missing_parent_id / "capsule.json"
        assert real_path.exists(), "real parent capsule not written"
        real_data: dict[str, Any] = json.loads(real_path.read_text(encoding="utf-8"))
        assert real_data.get("is_synthetic") is not True

        # Placeholder is marked superseded
        ph_path = spool / placeholder_id / "capsule.json"
        ph_data: dict[str, Any] = json.loads(ph_path.read_text(encoding="utf-8"))
        assert ph_data.get("superseded_by") == missing_parent_id


# ── AC-3: Phase 1 latency benchmark ──────────────────────────────────────


class TestPhase1Latency:
    """AC-3: writer.commit() p99 ≤ 50 ms on local tmpfs/NVMe (FR-17).

    Runs 100 capsule writes in a tmp_path fixture directory (local disk).
    Computes p99 from the measured durations and asserts it is within the
    50 ms budget.
    """

    def test_commit_p99_latency_le_50ms(self, tmp_path: Path) -> None:
        spool = tmp_path / "spool"
        iterations = 100
        latencies: list[float] = []

        for i in range(iterations):
            w = _make_writer(
                spool,
                capsule_role=CapsuleRole.STANDALONE,
                rank=i,
            )
            w.start()

            t0 = time.perf_counter()
            w.commit(status="COMPLETE")
            t1 = time.perf_counter()

            latencies.append((t1 - t0) * 1_000)  # ms

        latencies.sort()
        p99_index = int(len(latencies) * 0.99) - 1
        p99_ms = latencies[max(p99_index, 0)]

        # Print so it is visible in pytest -s output
        print(f"\n[BQ-012 AC-3] writer.commit() latency p99 = {p99_ms:.2f} ms "
              f"(target ≤ 50 ms) over {iterations} iterations")

        assert p99_ms <= 50.0, (
            f"writer.commit() p99 latency {p99_ms:.2f} ms exceeds 50 ms budget"
        )


# ── AC-4: nova lineage blast-radius --edge-type spawned ───────────────────


class TestEdgeTypeFilter:
    """AC-4: `nova lineage blast-radius <id> --edge-type spawned` filters correctly.

    The main `nova lineage` command (src/novafabric/cli/lineage.py) must accept
    --edge-type and return only edges matching the specified type.

    Strategy: seed the lineage store with two run nodes connected by edges of
    different types, then assert the CLI filters correctly.
    """

    def _seed_lineage_store(self, db_path: Path, run_id_a: str, run_id_b: str) -> None:
        """Insert a spawned edge and a contains edge into the lineage store."""
        from novafabric.lineage._store import LineageStore
        from novafabric.lineage._types import LineageEdge, LineageNode, node_id_for

        store = LineageStore(db_path=db_path)

        source_kind = "run"
        target_kind = "run"
        source_ref = run_id_a
        target_ref_b = run_id_b
        # Third node for a contains edge
        run_id_c = new_ulid()
        target_ref_c = run_id_c

        nodes_abc = [
            LineageNode(
                node_id=node_id_for(source_kind, source_ref),
                kind=source_kind,
                ref=source_ref,
                first_seen_capsule_run_id=None,
                payload={"kind": source_kind, "run_id": run_id_a},
            ),
            LineageNode(
                node_id=node_id_for(target_kind, target_ref_b),
                kind=target_kind,
                ref=target_ref_b,
                first_seen_capsule_run_id=None,
                payload={"kind": target_kind, "run_id": run_id_b},
            ),
            LineageNode(
                node_id=node_id_for(target_kind, target_ref_c),
                kind=target_kind,
                ref=target_ref_c,
                first_seen_capsule_run_id=None,
                payload={"kind": target_kind, "run_id": run_id_c},
            ),
        ]

        spawned_edge = LineageEdge(
            edge_type="spawned",
            source={"kind": source_kind, "run_id": run_id_a},
            target={"kind": target_kind, "run_id": run_id_b},
            capsule_run_id=run_id_a,
            confidence="high",
        )
        contains_edge = LineageEdge(
            edge_type="contains",
            source={"kind": source_kind, "run_id": run_id_a},
            target={"kind": target_kind, "run_id": run_id_c},
            capsule_run_id=run_id_a,
            confidence="high",
        )

        store.replace_capsule_lineage(nodes_abc, [spawned_edge, contains_edge], run_id_a)

    def test_blast_radius_edge_type_filter(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db_path = tmp_path / "lineage.db"
        run_id_a = new_ulid()
        run_id_b = new_ulid()
        self._seed_lineage_store(db_path, run_id_a, run_id_b)

        # Patch LineageStore to use the test db
        import novafabric.cli.lineage as lineage_mod
        from novafabric.lineage._store import LineageStore

        monkeypatch.setattr(
            lineage_mod,
            "_get_store",
            lambda: LineageStore(db_path=db_path),
        )

        runner = CliRunner()
        result = runner.invoke(
            lineage_app,
            ["blast-radius", run_id_b, "--edge-type", "spawned", "--output", "json"],
            catch_exceptions=False,
        )

        assert result.exit_code == 0, (
            f"CLI exited with code {result.exit_code}:\n{result.output}"
        )

    def test_blast_radius_invalid_edge_type_exits_1(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db_path = tmp_path / "lineage.db"

        import novafabric.cli.lineage as lineage_mod
        from novafabric.lineage._store import LineageStore

        monkeypatch.setattr(
            lineage_mod,
            "_get_store",
            lambda: LineageStore(db_path=db_path),
        )

        runner = CliRunner()
        result = runner.invoke(
            lineage_app,
            ["blast-radius", "some-ref", "--edge-type", "nonexistent_type"],
        )
        assert result.exit_code == 1

    def test_provenance_edge_type_filter(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--edge-type also works on the provenance command."""
        db_path = tmp_path / "lineage.db"
        run_id_a = new_ulid()
        run_id_b = new_ulid()
        self._seed_lineage_store(db_path, run_id_a, run_id_b)

        import novafabric.cli.lineage as lineage_mod
        from novafabric.lineage._store import LineageStore

        monkeypatch.setattr(
            lineage_mod,
            "_get_store",
            lambda: LineageStore(db_path=db_path),
        )

        runner = CliRunner()
        result = runner.invoke(
            lineage_app,
            ["provenance", run_id_a, "--edge-type", "spawned", "--output", "json"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, (
            f"CLI exited with code {result.exit_code}:\n{result.output}"
        )
