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

"""ADR-0171 P2 — NF-395 cross-run derivation + NF-396 provenance fan-out."""

from __future__ import annotations

import copy
import json
import random
from pathlib import Path
from typing import Any

import pytest

from novafabric.capture.events import MemoryOperationEvent
from novafabric.lineage.memory import edges_for_event
from novafabric.memstore import (
    MemstoreError,
    MutationActor,
    MutationRecord,
    append_mutation,
    assemble_ledger,
    attach_access,
    attach_derivation,
    build_access_block,
    collect_store_evidence,
    crosscheck_read_edges,
    derive_read,
    derive_reads,
    digest_value,
    ledger_from_sidecar,
    provenance_chain,
    record_access,
    record_digest,
    trace_derivation,
)
from novafabric.memstore import derivation as deriv_mod
from novafabric.memstore.access import AccessRecord
from novafabric.memstore.derivation import DerivationBoundError, LedgerIndex

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "memstore" / "valid-cross-run-derivation.json"
STORE = "org-kb/support-playbooks"
V1, V2 = digest_value("v1"), digest_value("v2")


@pytest.fixture(scope="module")
def doc() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text())


def _read(entry: str, run: str, **kw: Any) -> AccessRecord:
    base: dict[str, Any] = {
        "agent": "reader",
        "store_id": STORE,
        "namespace": "ns",
        "entry_id": entry,
        "access": "read",
        "allowed_scope": "ns",
        "run": run,
    }
    base.update(kw)
    return record_access([], **base)[0]


def _write(ledger: list[MutationRecord], entry: str, run: str, value: str | None, **kw: Any):
    return append_mutation(
        ledger,
        store_id=STORE,
        namespace="ns",
        entry_id=entry,
        op=kw.pop("op", "update" if ledger else "create"),
        by=MutationActor(agent="writer", run=run),
        at=kw.pop("at", "2026-07-01T00:00:00Z"),
        value_digest=value,
        **kw,
    )


# ── Golden fixture: the three-run story ───────────────────────────────────


def test_fixture_derive_resolves_across_runs(doc: dict[str, Any]) -> None:
    evidence = collect_store_evidence(doc["capsules"], STORE)
    assert not evidence.defective, evidence.findings
    assembly = assemble_ledger(evidence.mutations)
    assert assembly.chain_ok, assembly.reason
    trace = trace_derivation(
        store_id=STORE, entry_id="bl-7", run="run_C", assembly=assembly, accesses=evidence.accesses
    )
    expect = doc["expect"]["derive_bl7_in_run_C"]
    root = trace.hops[0]
    assert root.depth == 0 and root.link.status == "resolved"
    assert root.link.origin_mutation_ref == expect["origin_mutation_ref"]
    assert root.link.origin_run == expect["origin_run"]
    upstream = trace.hops[1]
    assert upstream.depth == 1 and upstream.parent_read_ref == root.link.read_ref
    assert upstream.link.origin_mutation_ref == expect["upstream_origin_mutation_ref"]
    assert upstream.link.origin_run == expect["upstream_origin_run"]
    assert not trace.truncated and len(trace.hops) == 2


def test_fixture_sidecar_ledger_matches_reassembly(doc: dict[str, Any]) -> None:
    evidence = collect_store_evidence(doc["capsules"], STORE)
    sidecar = ledger_from_sidecar([MutationRecord.model_validate(r) for r in doc["ledger"]])
    assert sidecar.chain_ok
    assert [record_digest(r) for r in sidecar.records] == [
        record_digest(r) for r in assemble_ledger(evidence.mutations).records
    ]


def test_fixture_provenance_fan_out(doc: dict[str, Any]) -> None:
    evidence = collect_store_evidence(doc["capsules"], STORE)
    chain = provenance_chain(
        store_id=STORE,
        entry_id="pb-4471",
        assembly=assemble_ledger(evidence.mutations),
        accesses=evidence.accesses,
    )
    expect = doc["expect"]["provenance_pb4471"]
    assert len(chain.writes) == 1
    node = chain.writes[0]
    assert node.seeded_by_mutation_ref == expect["seeded_by_mutation_ref"]
    assert node.source_ref == expect["source_ref"]
    assert node.read_by_runs == expect["read_by_runs"]
    assert node.read_count == 2 and chain.unresolved_reads == 0


def test_fixture_contains_uncontained_write_evidence(doc: dict[str, Any]) -> None:
    evidence = collect_store_evidence(doc["capsules"], STORE)
    out = [(a.run, a.namespace) for a in evidence.accesses if not a.contained]
    assert out == [("run_B", "billing")]


def test_fixture_carries_no_content(doc: dict[str, Any]) -> None:
    raw = FIXTURE.read_text()
    for needle in ("playbook v1", "billing note", "faq v9", "upstream source document"):
        assert needle not in raw


# ── Ledger assembly ───────────────────────────────────────────────────────


def test_assembly_is_order_independent_and_deduplicating() -> None:
    ledger: list[MutationRecord] = []
    for i in range(6):
        ledger = _write(ledger, f"e{i}", f"r{i}", digest_value(str(i)))
    shuffled = list(ledger) + [ledger[2]]
    random.Random(7).shuffle(shuffled)
    assembly = assemble_ledger(shuffled)
    assert assembly.chain_ok
    assert list(assembly.records) == ledger


def test_assembly_reports_a_gap_but_keeps_records() -> None:
    ledger: list[MutationRecord] = []
    for i in range(4):
        ledger = _write(ledger, f"e{i}", f"r{i}", digest_value(str(i)))
    assembly = assemble_ledger([ledger[0], ledger[2], ledger[3]])
    assert not assembly.chain_ok and "predecessor not supplied" in (assembly.reason or "")
    assert list(assembly.records) == [ledger[0], ledger[2], ledger[3]]


def test_assembly_reports_forks_and_missing_genesis() -> None:
    base = _write([], "e", "r0", V1)
    a = _write(base, "e", "r1", V2)[-1]
    b = _write(base, "e", "r2", digest_value("v3"))[-1]
    fork = assemble_ledger([base[0], a, b])
    assert not fork.chain_ok and "fork" in (fork.reason or "")
    assert len(fork.records) == 3
    headless = assemble_ledger([a])
    assert "0 genesis" in (headless.reason or "")


def test_assembly_terminates_on_forged_cycle() -> None:
    """Two records whose forged links point at each other cannot loop the walk."""
    r1 = MutationRecord(
        store_id=STORE,
        namespace="ns",
        entry_id="x",
        op="create",
        by=MutationActor(agent="a"),
        at="t1",
        value_digest=V1,
        prev_record_hash=digest_value("placeholder"),
    )
    r2 = r1.model_copy(update={"prev_record_hash": record_digest(r1), "at": "t2"})
    r1_cyclic = r1.model_copy(update={"prev_record_hash": record_digest(r2)})
    assembly = assemble_ledger([r1_cyclic, r2])
    assert len(assembly.records) == 2 and not assembly.chain_ok


def test_assembly_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(deriv_mod, "MAX_LEDGER_RECORDS", 1)
    ledger = _write(_write([], "a", "r", V1), "b", "r", V2)
    with pytest.raises(DerivationBoundError):
        assemble_ledger(ledger)
    with pytest.raises(DerivationBoundError):
        ledger_from_sidecar(ledger)


def test_sidecar_reports_broken_chain() -> None:
    ledger = _write(_write([], "a", "r", V1), "b", "r", V2)
    broken = ledger_from_sidecar([ledger[1], ledger[0]])
    assert not broken.chain_ok and "broken at record 0" in (broken.reason or "")


# ── NF-395 binding rules ──────────────────────────────────────────────────


def _index(ledger: list[MutationRecord]) -> LedgerIndex:
    return LedgerIndex(assemble_ledger(ledger))


def test_read_binds_to_most_recent_write_of_that_value() -> None:
    ledger = _write([], "e", "r1", V1)
    ledger = _write(ledger, "e", "r2", V2)
    ledger = _write(ledger, "e", "r3", V1, op="restore")  # V1 written again
    link = derive_read(_read("e", "r9", value_digest=V1), _index(ledger))
    assert link.status == "resolved" and link.origin_run == "r3" and link.candidates == 2


def test_honest_claim_among_candidates_is_honoured() -> None:
    ledger = _write([], "e", "r1", V1)
    ledger = _write(ledger, "e", "r3", V1)
    claim = record_digest(ledger[0])
    link = derive_read(
        _read("e", "r9", value_digest=V1, claimed_origin_mutation_ref=claim), _index(ledger)
    )
    assert link.status == "resolved" and link.origin_run == "r1"


def test_claim_contradicting_observed_value_is_a_mismatch() -> None:
    ledger = _write([], "e", "r1", V1)
    ledger = _write(ledger, "e", "r2", V2)
    link = derive_read(
        _read("e", "r9", value_digest=V2, claimed_origin_mutation_ref=record_digest(ledger[0])),
        _index(ledger),
    )
    assert link.status == "claim_mismatch" and link.origin_mutation_ref is None


def test_write_after_the_read_cannot_be_its_origin() -> None:
    ledger = _write([], "e", "r1", V1, at="2026-07-10T00:00:00Z")
    link = derive_read(_read("e", "r9", value_digest=V1, at="2026-07-09T00:00:00Z"), _index(ledger))
    assert link.status == "unresolved"
    # naive timestamps are treated as UTC, and unparseable ones as unknown
    ok = derive_read(_read("e", "r9", value_digest=V1, at="2026-07-11T00:00:00"), _index(ledger))
    assert ok.status == "resolved"
    unknown = derive_read(_read("e", "r9", value_digest=V1, at="yesterday"), _index(ledger))
    assert unknown.status == "resolved"


def test_value_nobody_recorded_writing_is_unresolved_not_invented() -> None:
    ledger = _write([], "e", "r1", V1)
    link = derive_read(_read("e", "r9", value_digest=V2), _index(ledger))
    assert link.status == "unresolved" and "out-of-band" in (link.reason or "")


def test_deleted_value_is_not_an_origin() -> None:
    ledger = _write([], "e", "r1", V1)
    ledger = _write(ledger, "e", "r2", None, op="delete", prev_value_digest=V1)
    link = derive_read(_read("e", "r9", value_digest=V1), _index(ledger))
    assert link.origin_run == "r1"


def test_digestless_read_rests_on_claim_or_is_unresolved() -> None:
    ledger = _write([], "e", "r1", V1)
    idx = _index(ledger)
    claimed = derive_read(
        _read("e", "r9", claimed_origin_mutation_ref=record_digest(ledger[0])), idx
    )
    assert claimed.status == "claimed_only" and claimed.origin_run == "r1"
    bogus = derive_read(_read("e", "r9", claimed_origin_mutation_ref=V2), idx)
    assert bogus.status == "claim_mismatch"
    nothing = derive_read(_read("e", "r9"), idx)
    assert nothing.status == "unresolved"


def test_write_rows_are_not_derived() -> None:
    write = _read("e", "r1", access="write")
    assert derive_read(write, _index([])).status == "unresolved"
    assert derive_reads([write, _read("e", "r2")], _index([]))[0].read_run == "r2"


# ── Multi-hop walk: cycle-safe, bounded, deterministic ────────────────────


def _cycle_world() -> tuple[Any, list[AccessRecord]]:
    """r1 reads b then writes a; r2 reads a then writes b — a derivation cycle."""
    ledger = _write([], "a", "r1", V1, at="x")
    ledger = _write(ledger, "b", "r2", V2, at="x")
    accesses = [
        _read("b", "r1", value_digest=V2),
        _read("a", "r2", value_digest=V1),
        _read("a", "r3", value_digest=V1),
    ]
    return assemble_ledger(ledger), accesses


def test_cycle_terminates_and_expands_each_read_once() -> None:
    assembly, accesses = _cycle_world()
    trace = trace_derivation(
        store_id=STORE, entry_id="a", run="r3", assembly=assembly, accesses=accesses
    )
    refs = [h.link.read_ref for h in trace.hops]
    assert len(refs) == len(set(refs)) == 3
    assert not trace.truncated


def test_depth_cap_truncates() -> None:
    assembly, accesses = _cycle_world()
    trace = trace_derivation(
        store_id=STORE, entry_id="a", run="r3", assembly=assembly, accesses=accesses, max_depth=0
    )
    assert len(trace.hops) == 1 and trace.truncated
    assert "depth cap" in (trace.truncated_reason or "")


def test_node_cap_truncates() -> None:
    assembly, accesses = _cycle_world()
    trace = trace_derivation(
        store_id=STORE, entry_id="a", run="r3", assembly=assembly, accesses=accesses, max_nodes=2
    )
    assert len(trace.hops) == 2 and trace.truncated
    assert "node cap" in (trace.truncated_reason or "")


@pytest.mark.parametrize(("depth", "nodes"), [(-1, 10), (65, 10), (3, 0), (3, 10_001)])
def test_walk_bounds_are_validated(depth: int, nodes: int) -> None:
    assembly, accesses = _cycle_world()
    with pytest.raises(DerivationBoundError):
        trace_derivation(
            store_id=STORE,
            entry_id="a",
            run="r3",
            assembly=assembly,
            accesses=accesses,
            max_depth=depth,
            max_nodes=nodes,
        )


def test_trace_is_deterministic_under_input_order() -> None:
    assembly, accesses = _cycle_world()
    one = trace_derivation(
        store_id=STORE, entry_id="a", run="r3", assembly=assembly, accesses=accesses
    )
    two = trace_derivation(
        store_id=STORE, entry_id="a", run="r3", assembly=assembly, accesses=accesses[::-1]
    )
    assert one.model_dump() == two.model_dump()


def test_upstream_excludes_reads_after_the_write_and_other_stores() -> None:
    ledger = _write([], "a", "r1", V1, at="2026-07-01T10:00:00Z")
    accesses = [
        _read("late", "r1", value_digest=V2, at="2026-07-01T11:00:00Z"),
        _read("other", "r1", store_id="other-store"),
        _read("a", "r1", value_digest=V1, at="2026-07-01T09:00:00Z"),  # own prior state
        _read("a", "r2", value_digest=V1, at="2026-07-02T00:00:00Z"),
    ]
    trace = trace_derivation(
        store_id=STORE, entry_id="a", run="r2", assembly=assemble_ledger(ledger), accesses=accesses
    )
    assert len(trace.hops) == 1


def test_trace_with_no_matching_read_is_empty() -> None:
    assembly, accesses = _cycle_world()
    trace = trace_derivation(
        store_id=STORE, entry_id="zzz", run="r3", assembly=assembly, accesses=accesses
    )
    assert trace.hops == []


def test_writer_without_run_stops_the_walk() -> None:
    ledger = append_mutation(
        [],
        store_id=STORE,
        namespace="ns",
        entry_id="a",
        op="create",
        by=MutationActor(agent="console-operator"),
        at="t",
        value_digest=V1,
    )
    trace = trace_derivation(
        store_id=STORE,
        entry_id="a",
        run="r9",
        assembly=assemble_ledger(ledger),
        accesses=[_read("a", "r9", value_digest=V1)],
    )
    assert len(trace.hops) == 1 and trace.hops[0].link.origin_run is None


# ── NF-396 ────────────────────────────────────────────────────────────────


def test_provenance_counts_unresolved_and_filters_namespace() -> None:
    ledger = _write([], "a", "r1", V1)
    ledger = _write(ledger, "a", "r2", None, op="delete")
    accesses = [
        _read("a", "r3", value_digest=V1),
        _read("a", "r4", value_digest=V2),
        _read("a", "r5", value_digest=V1, namespace="elsewhere"),
    ]
    chain = provenance_chain(
        store_id=STORE,
        entry_id="a",
        assembly=assemble_ledger(ledger),
        accesses=accesses,
        namespace="ns",
    )
    assert len(chain.writes) == 1 and chain.writes[0].read_by_runs == ["r3"]
    assert chain.unresolved_reads == 1


def test_provenance_ignores_malformed_source_ref_and_caps_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(deriv_mod, "MAX_FANOUT_RUNS", 2)
    record = _write([], "a", "r1", V1)[0]
    record = MutationRecord.model_validate(
        {**record.model_dump(exclude_none=True), "source_ref": ""}
    )
    accesses = [_read("a", f"r{i}", value_digest=V1) for i in range(5)]
    chain = provenance_chain(
        store_id=STORE, entry_id="a", assembly=assemble_ledger([record]), accesses=accesses
    )
    node = chain.writes[0]
    assert node.source_ref is None
    assert node.read_by_runs == ["r0", "r1"] and node.runs_truncated and node.read_count == 5


# ── Evidence collection ───────────────────────────────────────────────────


def test_collect_fills_run_and_flags_foreign_run_claims() -> None:
    rows = record_access(
        [],
        agent="a",
        store_id=STORE,
        namespace="ns",
        entry_id="e",
        access="read",
        allowed_scope="ns",
    )
    rows = record_access(
        rows,
        agent="a",
        store_id=STORE,
        namespace="ns",
        entry_id="f",
        access="read",
        allowed_scope="ns",
        run="someone-else",
    )
    capsule = attach_access({"run_id": "r1"}, build_access_block(STORE, rows))
    evidence = collect_store_evidence([capsule], STORE)
    assert [a.run for a in evidence.accesses] == ["r1"]
    assert evidence.defective and "claims run" in evidence.findings[0]


def test_collect_flags_malformed_and_tampered_blocks(doc: dict[str, Any]) -> None:
    good = doc["capsules"][1]
    bad_facet = {"run_id": "x", "facets": {"memstore_mutation": {"store_id": ""}}}
    bad_access = {
        "run_id": "y",
        "facets": {
            "memstore_mutation": {"store_id": STORE, "access": {"store_id": STORE, "text": "x"}}
        },
    }
    tampered = json.loads(json.dumps(good))
    tampered["facets"]["memstore_mutation"]["access"]["accesses"].reverse()
    forged = json.loads(json.dumps(good))
    forged_block = forged["facets"]["memstore_mutation"]["access"]
    forged_block["accesses"] = forged_block["accesses"][:1]
    forged_block["accesses"][0]["contained"] = False
    forged_block["ledger_ref"] = deriv_mod.access_digest(
        AccessRecord.model_validate(forged_block["accesses"][0])
    )
    for capsule, needle in [
        (bad_facet, "malformed memstore_mutation"),
        (bad_access, "malformed access block"),
        (tampered, "access chain broken"),
        (forged, "contained flag"),
    ]:
        evidence = collect_store_evidence([capsule], STORE)
        assert evidence.defective and needle in evidence.findings[0], evidence.findings
        assert evidence.accesses == []


def test_collect_ignores_other_stores_and_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = record_access(
        [],
        agent="a",
        store_id="other",
        namespace="ns",
        entry_id="e",
        access="read",
        allowed_scope="*",
    )
    capsule = attach_access({}, build_access_block("other", rows))
    evidence = collect_store_evidence([capsule, {"facets": "nonsense"}], STORE)
    assert evidence.accesses == [] and not evidence.defective
    monkeypatch.setattr(deriv_mod, "MAX_CAPSULES", 1)
    with pytest.raises(DerivationBoundError):
        collect_store_evidence([{}, {}], STORE)
    monkeypatch.setattr(deriv_mod, "MAX_CAPSULES", 10)
    monkeypatch.setattr(deriv_mod, "MAX_TOTAL_ACCESSES", 0)
    with pytest.raises(DerivationBoundError):
        collect_store_evidence(
            [attach_access({"run_id": "r"}, build_access_block("other", rows))], "other"
        )


def test_collect_rejects_bad_store_id() -> None:
    with pytest.raises(MemstoreError):
        collect_store_evidence([], "")


# ── C2 composition: read existing edges, emit none ────────────────────────


def test_crosscheck_against_shipped_read_edges() -> None:
    ledger = _write([], "a", "r1", V1)
    idx = _index(ledger)
    links = derive_reads(
        [_read("a", "r2", value_digest=V1), _read("a", "r3", value_digest=V1), _read("a", "r4")],
        idx,
    )

    def edge(run: str, origin: str | None):
        return edges_for_event(
            MemoryOperationEvent(
                run_id=run,
                capsule_id="c",
                timestamp_utc="t",
                operation="read",
                memory_key="a",
                origin_run_id=origin,
            ),
            namespace="ns",
        )[0]

    edges = [edge("r2", "r1"), edge("r3", "r0"), edge("r4", None)]
    edges.append(
        edges_for_event(
            MemoryOperationEvent(
                run_id="r1", capsule_id="c", timestamp_utc="t", operation="write", memory_key="a"
            ),
            namespace="ns",
        )[0]
    )
    before = copy.deepcopy(edges)
    checks = crosscheck_read_edges(links, edges)
    assert [(c.read_run, c.agrees) for c in checks] == [("r2", True), ("r3", False)]
    assert edges == before  # nothing emitted or mutated
    unrun = links[0].model_copy(update={"read_run": None})
    assert crosscheck_read_edges([unrun], edges) == []


# ── Facet attachment of derivation links ──────────────────────────────────


def test_attach_derivation(doc: dict[str, Any]) -> None:
    evidence = collect_store_evidence(doc["capsules"], STORE)
    links = derive_reads(evidence.accesses, LedgerIndex(assemble_ledger(evidence.mutations)))
    capsule = doc["capsules"][2]
    assert attach_derivation(capsule, []) is capsule
    out = attach_derivation(capsule, links)
    stored = out["facets"]["memstore_mutation"]["derivation"]
    assert len(stored) == len(links) and "derivation" not in capsule["facets"]["memstore_mutation"]
    with pytest.raises(MemstoreError):
        attach_derivation({"run_id": "r"}, links)
    other = links[0].model_copy(update={"store_id": "other"})
    with pytest.raises(MemstoreError):
        attach_derivation(capsule, [other])
