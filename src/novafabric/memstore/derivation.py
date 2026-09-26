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

"""Cross-run memory derivation + org-knowledge provenance — ADR-0171 D2/P2
(NF-395, NF-396).

**NF-395 back-trace.** Each read recorded in the NF-392 access ledger is traced
to the NF-391 store-write that seeded it: its ``origin_mutation_ref`` (the
mutation record's chain digest) and ``origin_run``. This is the at-rest
analogue of C2's within-run grounding: a bad claim in today's run resolves to
the write, months ago and in another run, that put the value there.

**NF-396 fan-out.** The inverse view of one entry:
``source → store-write → many later runs`` — for every write of the entry, its
declared upstream ``source_ref`` (when the writer recorded one) and every run
whose reads resolve to that write.

How a read is bound to a write
------------------------------
By **content identity**, never by guesswork. A read that recorded the
``value_digest`` it observed binds to the mutation records of the same
``store_id``/``namespace``/``entry_id`` whose post-op ``value_digest`` equals
it, excluding any write whose timestamp is provably *after* the read. The most
recent such write in ledger order is the origin. A reader's own
``claimed_origin_mutation_ref`` is honoured only if it is one of those
candidates; a claim that contradicts the observed digest is reported as
``claim_mismatch``, not believed. A read with no digest and no claim is
``unresolved`` — recorded coverage, never a fabricated origin. A read of a value
no recorded mutation produced (an out-of-band write) is also ``unresolved``:
the ledger is only as complete as what the store emitted (ADR-0171
"Negative / trade-offs").

Multi-hop trace (``nova memstore derive``)
------------------------------------------
From a read in run *R*, the walk continues into the origin run *R1*: the reads
*R1* made before its write (by timestamp, when both are known) are its
*upstream inputs*, and each is traced in turn. That hop is run-level
**co-occurrence**, not proven causation — *R1* read those entries and then
wrote; NovaFabric does not claim the write was computed from them.

The walk is breadth-first, **cycle-safe** (every read row is expanded at most
once, keyed on its chain digest, so ``R1 → R2 → R1`` terminates), and
**bounded** by a depth cap and a node cap; hitting either sets ``truncated``
with the reason. Output ordering is fully deterministic.

Input: an explicit list of capsules, read-only
----------------------------------------------
There is no cross-run index in NovaFabric (and ADR-0171 P2 does not create
one). Everything here runs over the capsules the caller supplies: the
mutation records each capsule's facet contributed (``records_in_this_run``) are
re-assembled into the store's chain by ``prev_record_hash`` links, and each
capsule's access rows are attributed to that capsule's ``run_id``. A capsule
set that omits an intermediate run yields a partial chain, which is reported
(``chain_ok: false``) rather than silently repaired. An explicit sealed ledger
sidecar may be supplied instead of re-assembly.

Composition with C2: :func:`crosscheck_read_edges` compares each derived
origin against the ``claimed_origin_run_id`` the shipped per-run
``read_memory`` lineage edges (``lineage/memory.py``) already carry. It reads
those edges; it never emits new ones.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from novafabric.memstore.access import (
    AccessLedgerBlock,
    AccessRecord,
    access_block_from_capsule,
    access_digest,
    verify_access_block,
)
from novafabric.memstore.ledger import (
    FACET_NAME,
    MemstoreError,
    MutationRecord,
    _validate_id,
    facet_from_capsule,
    record_digest,
    verify_chain,
)

if TYPE_CHECKING:
    from novafabric.lineage._types import LineageEdge

DERIVATION_BLOCK = "derivation"

#: Walk bounds. Defaults are generous for an incident trace and small enough
#: that an adversarial capsule set cannot turn a trace into a denial of service.
DEFAULT_MAX_DEPTH = 8
MAX_DEPTH_LIMIT = 64
DEFAULT_MAX_NODES = 500
MAX_NODES_LIMIT = 10_000
#: Per-write cap on the runs listed in an NF-396 fan-out.
MAX_FANOUT_RUNS = 1_000
#: Bounds on what a capsule set may contribute.
MAX_CAPSULES = 512
MAX_LEDGER_RECORDS = 100_000
MAX_TOTAL_ACCESSES = 200_000

DerivationStatus = Literal["resolved", "claimed_only", "unresolved", "claim_mismatch"]
_RESOLVED: frozenset[str] = frozenset({"resolved", "claimed_only"})

# ── Errors ────────────────────────────────────────────────────────────────


class DerivationBoundError(MemstoreError):
    """Raised when inputs or walk bounds exceed the documented limits."""


# ── Result objects ────────────────────────────────────────────────────────


class DerivationLink(BaseModel):
    """One read, back-traced to the write that seeded it (NF-395)."""

    model_config = ConfigDict(extra="forbid")

    store_id: str
    namespace: str
    read_entry: str
    read_run: str | None = None
    reader_agent: str
    read_ref: str
    read_value_digest: str | None = None
    status: DerivationStatus
    origin_mutation_ref: str | None = None
    origin_run: str | None = None
    origin_agent: str | None = None
    origin_at: str | None = None
    candidates: int = 0
    reason: str | None = None


class TraceHop(BaseModel):
    """A node in the multi-hop derivation walk."""

    model_config = ConfigDict(extra="forbid")

    depth: int
    parent_read_ref: str | None = None
    link: DerivationLink


class DerivationTrace(BaseModel):
    """The bounded, cycle-safe back-trace from one entry read in one run."""

    model_config = ConfigDict(extra="forbid")

    store_id: str
    entry_id: str
    run: str
    hops: list[TraceHop] = Field(default_factory=list)
    truncated: bool = False
    truncated_reason: str | None = None
    max_depth: int
    max_nodes: int
    ledger_chain_ok: bool
    ledger_reason: str | None = None


class ProvenanceNode(BaseModel):
    """One write of an entry and the later runs that read it (NF-396)."""

    model_config = ConfigDict(extra="forbid")

    seeded_by_mutation_ref: str
    op: str
    source_ref: str | None = None
    writer_run: str | None = None
    writer_agent: str
    at: str
    read_by_runs: list[str] = Field(default_factory=list)
    read_count: int = 0
    runs_truncated: bool = False


class ProvenanceChain(BaseModel):
    """``source → store-write → many later runs`` for one entry (NF-396)."""

    model_config = ConfigDict(extra="forbid")

    store_id: str
    entry_id: str
    namespace: str | None = None
    writes: list[ProvenanceNode] = Field(default_factory=list)
    unresolved_reads: int = 0
    ledger_chain_ok: bool
    ledger_reason: str | None = None


class EdgeCrosscheck(BaseModel):
    """A derived origin compared with a C2 ``read_memory`` edge's claim."""

    model_config = ConfigDict(extra="forbid")

    read_ref: str
    read_run: str
    claimed_origin_run: str
    derived_origin_run: str | None = None
    agrees: bool


# ── Ledger assembly ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class LedgerAssembly:
    """Mutation records ordered by their chain links, with a completeness verdict."""

    records: tuple[MutationRecord, ...]
    chain_ok: bool
    reason: str | None = None


def _ts_key(record: MutationRecord, digest: str) -> tuple[str, str]:
    return (record.at, digest)


def assemble_ledger(records: Sequence[MutationRecord]) -> LedgerAssembly:
    """Order *records* by ``prev_record_hash`` links, deduplicating by digest.

    Deterministic for any input order. ``chain_ok`` is True only when the set
    is exactly one unforked chain from a genesis record; otherwise the records
    are still returned (reachable runs first, each started from a root sorted
    by ``(at, digest)``), with the reason — a gap from an omitted capsule, a
    fork, or a tampered link — so a trace can proceed over partial evidence
    while saying it is partial.

    Raises:
        DerivationBoundError: over :data:`MAX_LEDGER_RECORDS` records.
    """
    if len(records) > MAX_LEDGER_RECORDS:
        raise DerivationBoundError(f"over {MAX_LEDGER_RECORDS} mutation records")
    by_digest: dict[str, MutationRecord] = {}
    for record in records:
        by_digest.setdefault(record_digest(record), record)
    successors: dict[str | None, list[str]] = defaultdict(list)
    for digest, record in by_digest.items():
        successors[record.prev_record_hash].append(digest)
    for children in successors.values():
        children.sort(key=lambda d: _ts_key(by_digest[d], d))

    roots = sorted(
        (d for d, r in by_digest.items() if r.prev_record_hash not in by_digest),
        key=lambda d: (by_digest[d].prev_record_hash is not None, *_ts_key(by_digest[d], d)),
    )
    ordered: list[str] = []
    seen: set[str] = set()
    for root in roots:
        stack = [root]
        while stack:
            digest = stack.pop()
            if digest in seen:
                continue
            seen.add(digest)
            ordered.append(digest)
            stack.extend(reversed(successors.get(digest, [])))
    # Anything unreached sits on a cycle (impossible for honest digests, but a
    # forged prev link can make one); append deterministically, never loop.
    ordered.extend(sorted((d for d in by_digest if d not in seen), key=lambda d: d))

    reasons: list[str] = []
    geneses = [d for d in roots if by_digest[d].prev_record_hash is None]
    if len(geneses) != 1:
        reasons.append(f"{len(geneses)} genesis records (expected 1)")
    dangling = len(roots) - len(geneses)
    if dangling:
        reasons.append(
            f"{dangling} record(s) link to a predecessor not supplied — a capsule "
            "is missing from the set, or a link was altered"
        )
    forks = sum(1 for kids in successors.values() if len(kids) > 1)
    if forks:
        reasons.append(f"{forks} fork(s): two records claim the same predecessor")
    if len(seen) != len(by_digest):
        reasons.append("records unreachable from any root (cyclic links)")
    ordered_records = tuple(by_digest[d] for d in ordered)
    return LedgerAssembly(
        records=ordered_records,
        chain_ok=not reasons,
        reason="; ".join(reasons) or None,
    )


def ledger_from_sidecar(records: Sequence[MutationRecord]) -> LedgerAssembly:
    """Wrap an explicit, already-ordered ledger (the sealed sidecar)."""
    if len(records) > MAX_LEDGER_RECORDS:
        raise DerivationBoundError(f"over {MAX_LEDGER_RECORDS} mutation records")
    result = verify_chain(records)
    reason = None if result.ok else f"broken at record {result.broken_at}: {result.reason}"
    return LedgerAssembly(records=tuple(records), chain_ok=result.ok, reason=reason)


# ── Evidence collection from capsules ─────────────────────────────────────


@dataclass
class StoreEvidence:
    """Everything a set of capsules recorded about one store."""

    store_id: str
    mutations: list[MutationRecord] = field(default_factory=list)
    accesses: list[AccessRecord] = field(default_factory=list)
    findings: list[str] = field(default_factory=list)
    defective: bool = False


def _capsule_label(manifest: Mapping[str, Any], position: int) -> str:
    run_id = manifest.get("run_id")
    return run_id if isinstance(run_id, str) and run_id else f"capsule[{position}]"


def collect_store_evidence(manifests: Sequence[Mapping[str, Any]], store_id: str) -> StoreEvidence:
    """Collect the NF-391 mutations and NF-392 accesses for *store_id*.

    Read-only over already-parsed capsule manifests. Each access row is
    attributed to its capsule's ``run_id``; a row claiming another run is a
    finding and is excluded (evidence one capsule cannot vouch for). A
    malformed facet or access block, a broken access chain, or a forged scope
    flag marks the evidence ``defective`` — reported, and the offending
    capsule's rows are excluded rather than trusted.

    Raises:
        DerivationBoundError: over :data:`MAX_CAPSULES` capsules or
            :data:`MAX_TOTAL_ACCESSES` access rows.
    """
    _validate_id(store_id, field="store_id")
    if len(manifests) > MAX_CAPSULES:
        raise DerivationBoundError(f"over {MAX_CAPSULES} capsules supplied")
    evidence = StoreEvidence(store_id=store_id)
    for position, manifest in enumerate(manifests):
        label = _capsule_label(manifest, position)
        run_id = manifest.get("run_id") if isinstance(manifest.get("run_id"), str) else None
        _collect_mutations(manifest, store_id, label, evidence)
        _collect_accesses(manifest, store_id, label, run_id, evidence)
        if len(evidence.accesses) > MAX_TOTAL_ACCESSES:
            raise DerivationBoundError(f"over {MAX_TOTAL_ACCESSES} access rows")
    return evidence


def _collect_mutations(
    manifest: Mapping[str, Any], store_id: str, label: str, evidence: StoreEvidence
) -> None:
    try:
        facet = facet_from_capsule(dict(manifest))
    except (ValidationError, MemstoreError) as exc:
        evidence.defective = True
        evidence.findings.append(f"{label}: malformed {FACET_NAME} facet ({type(exc).__name__})")
        return
    if facet is None or facet.store_id != store_id:
        return
    evidence.mutations.extend(facet.records_in_this_run)


def _collect_accesses(
    manifest: Mapping[str, Any],
    store_id: str,
    label: str,
    run_id: str | None,
    evidence: StoreEvidence,
) -> None:
    try:
        block: AccessLedgerBlock | None = access_block_from_capsule(manifest)
    except (ValidationError, MemstoreError) as exc:
        evidence.defective = True
        evidence.findings.append(f"{label}: malformed access block ({type(exc).__name__})")
        return
    if block is None or block.store_id != store_id:
        return
    verified = verify_access_block(block)
    if not verified.chain_ok:
        evidence.defective = True
        evidence.findings.append(f"{label}: access chain broken — {verified.reason}")
        return
    if not verified.scope_flags_ok:
        evidence.defective = True
        evidence.findings.append(f"{label}: a recorded contained flag disagrees with its scope")
        return
    for index, record in enumerate(block.accesses):
        if record.run is not None and run_id is not None and record.run != run_id:
            evidence.defective = True
            evidence.findings.append(
                f"{label}: access row {index} claims run {record.run!r}; excluded"
            )
            continue
        if record.run is None and run_id is not None:
            record = record.model_copy(update={"run": run_id})
        evidence.accesses.append(record)


# ── NF-395: one read → its origin write ───────────────────────────────────


def _parse_ts(value: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp; None when absent or unparseable."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


@dataclass(frozen=True)
class _Write:
    position: int
    digest: str
    record: MutationRecord


class LedgerIndex:
    """Mutation records indexed by entry, in ledger order. Built once per walk."""

    def __init__(self, assembly: LedgerAssembly) -> None:
        """Index *assembly*'s records by ``(store_id, namespace, entry_id)``."""
        self.assembly = assembly
        self._by_entry: dict[tuple[str, str, str], list[_Write]] = defaultdict(list)
        self._by_digest: dict[str, _Write] = {}
        for position, record in enumerate(assembly.records):
            write = _Write(position, record_digest(record), record)
            self._by_entry[(record.store_id, record.namespace, record.entry_id)].append(write)
            self._by_digest[write.digest] = write

    def writes_of(self, store_id: str, namespace: str, entry_id: str) -> list[_Write]:
        """Return the recorded mutations of one entry, in ledger order."""
        return list(self._by_entry.get((store_id, namespace, entry_id), ()))

    def get(self, digest: str) -> _Write | None:
        """Return the mutation with chain digest *digest*, if recorded."""
        return self._by_digest.get(digest)


def _link(
    read: AccessRecord,
    status: DerivationStatus,
    *,
    origin: _Write | None = None,
    candidates: int = 0,
    reason: str | None = None,
) -> DerivationLink:
    return DerivationLink(
        store_id=read.store_id,
        namespace=read.namespace,
        read_entry=read.entry_id,
        read_run=read.run,
        reader_agent=read.agent,
        read_ref=access_digest(read),
        read_value_digest=read.value_digest,
        status=status,
        origin_mutation_ref=origin.digest if origin else None,
        origin_run=origin.record.by.run if origin else None,
        origin_agent=origin.record.by.agent if origin else None,
        origin_at=origin.record.at if origin else None,
        candidates=candidates,
        reason=reason,
    )


def derive_read(read: AccessRecord, index: LedgerIndex) -> DerivationLink:
    """Back-trace one read to its seeding write (NF-395). Never raises.

    See the module docstring for the binding rule. A ``write`` row is not a
    read; passing one returns ``unresolved`` with that reason.
    """
    if read.access != "read":
        return _link(read, "unresolved", reason="not a read access")
    writes = [
        w
        for w in index.writes_of(read.store_id, read.namespace, read.entry_id)
        if w.record.op != "delete"
    ]
    read_at = _parse_ts(read.at)
    if read_at is not None:
        writes = [w for w in writes if (w_at := _parse_ts(w.record.at)) is None or w_at <= read_at]
    claim = read.claimed_origin_mutation_ref

    if read.value_digest is None:
        claimed = next((w for w in writes if w.digest == claim), None) if claim else None
        if claimed is not None:
            return _link(
                read,
                "claimed_only",
                origin=claimed,
                candidates=1,
                reason="read recorded no value_digest; origin rests on the reader's claim",
            )
        if claim:
            return _link(
                read,
                "claim_mismatch",
                reason="claimed origin is not a recorded write of this entry before the read",
            )
        return _link(
            read,
            "unresolved",
            reason="read recorded neither a value_digest nor a claimed origin",
        )

    candidates = [w for w in writes if w.record.value_digest == read.value_digest]
    if not candidates:
        return _link(
            read,
            "unresolved",
            reason=(
                "no recorded write produced the observed value_digest (an "
                "out-of-band write, or a capsule missing from the set)"
            ),
        )
    if claim is not None:
        claimed = next((w for w in candidates if w.digest == claim), None)
        if claimed is None:
            return _link(
                read,
                "claim_mismatch",
                candidates=len(candidates),
                reason="the reader's claimed origin did not produce the value it observed",
            )
        return _link(read, "resolved", origin=claimed, candidates=len(candidates))
    return _link(read, "resolved", origin=candidates[-1], candidates=len(candidates))


def derive_reads(accesses: Sequence[AccessRecord], index: LedgerIndex) -> list[DerivationLink]:
    """Back-trace every read in *accesses*, in input order."""
    return [derive_read(a, index) for a in accesses if a.access == "read"]


# ── NF-395 multi-hop walk ─────────────────────────────────────────────────


def _check_bounds(max_depth: int, max_nodes: int) -> None:
    if not 0 <= max_depth <= MAX_DEPTH_LIMIT:
        raise DerivationBoundError(f"max_depth must be within 0..{MAX_DEPTH_LIMIT}")
    if not 1 <= max_nodes <= MAX_NODES_LIMIT:
        raise DerivationBoundError(f"max_nodes must be within 1..{MAX_NODES_LIMIT}")


def _read_sort_key(read: AccessRecord) -> tuple[str, str, str, str]:
    return (read.run or "", read.namespace, read.entry_id, access_digest(read))


def _upstream_reads(
    origin: DerivationLink, reads_by_run: Mapping[str, list[AccessRecord]]
) -> list[AccessRecord]:
    """Reads the origin run made before its write (co-occurrence, not causation)."""
    if origin.origin_run is None:
        return []
    write_at = _parse_ts(origin.origin_at)
    out = []
    for read in reads_by_run.get(origin.origin_run, ()):
        if read.store_id != origin.store_id:
            continue
        # A run's read of the very entry it then wrote is its own prior state,
        # not an upstream input; following it would loop on one entry.
        if (read.namespace, read.entry_id) == (origin.namespace, origin.read_entry):
            continue
        read_at = _parse_ts(read.at)
        if write_at is not None and read_at is not None and read_at > write_at:
            continue
        out.append(read)
    return sorted(out, key=_read_sort_key)


def trace_derivation(
    *,
    store_id: str,
    entry_id: str,
    run: str,
    assembly: LedgerAssembly,
    accesses: Sequence[AccessRecord],
    namespace: str | None = None,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_nodes: int = DEFAULT_MAX_NODES,
) -> DerivationTrace:
    """Walk ``read in run → origin write → origin run's earlier reads → …``.

    Breadth-first, cycle-safe (each read row expanded once, keyed on its chain
    digest) and bounded by *max_depth* hops and *max_nodes* links. Depth 0 is
    the root read(s) of *entry_id* by *run*; an empty ``hops`` list means the
    run recorded no such read.

    Raises:
        DerivationBoundError: if a bound is outside its documented range.
    """
    _check_bounds(max_depth, max_nodes)
    index = LedgerIndex(assembly)
    reads_by_run: dict[str, list[AccessRecord]] = defaultdict(list)
    for access in accesses:
        if access.access == "read" and access.run is not None:
            reads_by_run[access.run].append(access)

    roots = [
        r
        for r in reads_by_run.get(run, ())
        if r.store_id == store_id
        and r.entry_id == entry_id
        and (namespace is None or r.namespace == namespace)
    ]
    trace = DerivationTrace(
        store_id=store_id,
        entry_id=entry_id,
        run=run,
        max_depth=max_depth,
        max_nodes=max_nodes,
        ledger_chain_ok=assembly.chain_ok,
        ledger_reason=assembly.reason,
    )
    frontier: list[tuple[AccessRecord, str | None]] = [
        (r, None) for r in sorted(roots, key=_read_sort_key)
    ]
    visited: set[str] = set()
    depth = 0
    while frontier:
        next_frontier: list[tuple[AccessRecord, str | None]] = []
        for read, parent in frontier:
            ref = access_digest(read)
            if ref in visited:
                continue
            if len(trace.hops) >= max_nodes:
                trace.truncated = True
                trace.truncated_reason = f"node cap {max_nodes} reached"
                return trace
            visited.add(ref)
            link = derive_read(read, index)
            trace.hops.append(TraceHop(depth=depth, parent_read_ref=parent, link=link))
            if link.status in _RESOLVED:
                next_frontier.extend(
                    (up, ref)
                    for up in _upstream_reads(link, reads_by_run)
                    if access_digest(up) not in visited
                )
        if next_frontier and depth >= max_depth:
            trace.truncated = True
            trace.truncated_reason = f"depth cap {max_depth} reached"
            return trace
        frontier = next_frontier
        depth += 1
    return trace


# ── NF-396: source → store-write → many later runs ────────────────────────


def _source_ref(record: MutationRecord) -> str | None:
    """Return a writer-recorded upstream ``source_ref`` extra, if well-formed."""
    extra = record.model_extra or {}
    value = extra.get("source_ref")
    if value is None:
        return None
    try:
        return _validate_id(value, field="source_ref")
    except MemstoreError:
        return None


def provenance_chain(
    *,
    store_id: str,
    entry_id: str,
    assembly: LedgerAssembly,
    accesses: Sequence[AccessRecord],
    namespace: str | None = None,
) -> ProvenanceChain:
    """Build the NF-396 fan-out for one entry. Deterministic; never raises.

    ``writes`` follow ledger order; each lists the sorted, de-duplicated runs
    whose reads resolve to it (capped at :data:`MAX_FANOUT_RUNS`, with
    ``runs_truncated``). Reads of the entry that resolve to no write are
    counted in ``unresolved_reads``, never attributed.
    """
    index = LedgerIndex(assembly)
    chain = ProvenanceChain(
        store_id=store_id,
        entry_id=entry_id,
        namespace=namespace,
        ledger_chain_ok=assembly.chain_ok,
        ledger_reason=assembly.reason,
    )
    runs_by_ref: dict[str, set[str]] = defaultdict(set)
    counts: dict[str, int] = defaultdict(int)
    for access in accesses:
        if (
            access.access != "read"
            or access.store_id != store_id
            or access.entry_id != entry_id
            or (namespace is not None and access.namespace != namespace)
        ):
            continue
        link = derive_read(access, index)
        if link.status in _RESOLVED and link.origin_mutation_ref is not None:
            counts[link.origin_mutation_ref] += 1
            if access.run is not None:
                runs_by_ref[link.origin_mutation_ref].add(access.run)
        else:
            chain.unresolved_reads += 1

    for record in assembly.records:
        if (
            record.store_id != store_id
            or record.entry_id != entry_id
            or (namespace is not None and record.namespace != namespace)
            or record.op == "delete"
        ):
            continue
        ref = record_digest(record)
        runs = sorted(runs_by_ref.get(ref, ()))
        chain.writes.append(
            ProvenanceNode(
                seeded_by_mutation_ref=ref,
                op=record.op,
                source_ref=_source_ref(record),
                writer_run=record.by.run,
                writer_agent=record.by.agent,
                at=record.at,
                read_by_runs=runs[:MAX_FANOUT_RUNS],
                read_count=counts.get(ref, 0),
                runs_truncated=len(runs) > MAX_FANOUT_RUNS,
            )
        )
    return chain


# ── C2 composition: compare with shipped read_memory edges ────────────────


def crosscheck_read_edges(
    links: Sequence[DerivationLink], edges: Sequence[LineageEdge]
) -> list[EdgeCrosscheck]:
    """Compare derived origins with C2 ``read_memory`` edges' claimed origins.

    Reads the edges ``lineage/memory.py`` already builds (source = memory node
    for ``namespace:entry``, target = reading run, facet
    ``claimed_origin_run_id``). Emits nothing into the graph. Only edges that
    carry a claim are compared; the result is sorted for determinism.
    """
    from novafabric.lineage._types import node_id_for
    from novafabric.lineage.memory import MEMORY_NODE_KIND, node_ref_for_memory

    claims: dict[tuple[str, str], str] = {}
    for edge in edges:
        if edge.edge_type != "read_memory":
            continue
        claimed = (edge.facets or {}).get("claimed_origin_run_id")
        run_id = edge.target.get("run_id")
        node_id = edge.source.get("node_id")
        if isinstance(claimed, str) and isinstance(run_id, str) and isinstance(node_id, str):
            claims[(node_id, run_id)] = claimed

    out: list[EdgeCrosscheck] = []
    for link in links:
        if link.read_run is None:
            continue
        node_id = node_id_for(
            MEMORY_NODE_KIND, node_ref_for_memory(link.read_entry, namespace=link.namespace)
        )
        claimed = claims.get((node_id, link.read_run))
        if claimed is None:
            continue
        out.append(
            EdgeCrosscheck(
                read_ref=link.read_ref,
                read_run=link.read_run,
                claimed_origin_run=claimed,
                derived_origin_run=link.origin_run,
                agrees=claimed == link.origin_run,
            )
        )
    return sorted(out, key=lambda c: (c.read_run, c.read_ref))


# ── Facet attachment ──────────────────────────────────────────────────────


def attach_derivation(capsule: dict[str, Any], links: Sequence[DerivationLink]) -> dict[str, Any]:
    """Store *links* under ``facets.memstore_mutation.derivation``, additively.

    No links ⇒ *capsule* returned unchanged (I-3). Requires the facet to exist
    already (attach the mutation or access block first): a derivation with no
    store to belong to would be a claim about nothing.

    Raises:
        MemstoreError: if the capsule has no ``memstore_mutation`` facet, or a
            link names a different store from the facet.
    """
    if not links:
        return capsule
    facets = capsule.get("facets")
    facet = facets.get(FACET_NAME) if isinstance(facets, dict) else None
    if not isinstance(facet, dict):
        raise MemstoreError(f"attach the {FACET_NAME} facet before its derivation block")
    if any(link.store_id != facet.get("store_id") for link in links):
        raise MemstoreError("a derivation link names a store other than the facet's")
    out = dict(capsule)
    new_facets = dict(facets)  # type: ignore[arg-type]  # checked dict above
    new_facet = dict(facet)
    new_facet[DERIVATION_BLOCK] = [link.model_dump(exclude_none=True) for link in links]
    new_facets[FACET_NAME] = new_facet
    out["facets"] = new_facets
    return out
