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

"""KB-mutation ledger + facet — ADR-0171 D1/P1 (NF-391).

The store-axis layer. A run capsule records what one run did; the shared
knowledge base the whole fleet reads outlives every run, and the 2026 memory
frameworks leave *who changed this entry, what changed, and when* unanswered.
This module writes the ordered, hash-chained **mutation ledger** that answers
those three questions, plus the per-capsule ``facets.memstore_mutation`` that
references it by digest.

Four invariants from ADR-0171 shape every choice in this module:

- **I-1 Record-only, store-external.** NovaFabric records evidence *about* an
  external store. Nothing here reads, writes, hosts, serves, or manages the
  store, and nothing here asserts a mutation was authorised, correct, or
  benign — only that it was recorded.
- **I-2 References and digests only.** ``who / what / when`` and nothing else.
  The mutated value is bound by ``value_digest``; the entry's content, its
  embedding vector, and any PII in it never enter the ledger (ADR-0009,
  ADR-0021 §4).
- **I-3 Fail-open.** No store material ⇒ no facet, no ledger row — never an
  exception, never a blocked workload. A store-less capsule is returned
  byte-identical.
- **I-4 Append-only.** A record is never rewritten. Tampering with, reordering,
  or dropping a record breaks the chain and is detectable offline, at a
  reported position.

Hash construction — a CHAIN, not a tree
---------------------------------------
The ADR says "NovaSeal-hash-chained". That is a *chain*: each record digests
its predecessor's digest. It is deliberately **not** a Merkle tree, and this
module computes none.

The repository carries two mutually incompatible Merkle constructions —
``evidence/merkle.py`` (RFC 6962, domain-separated leaf/inner prefixes) and
``trust/novaseal/merkle.py`` (pairwise with odd-duplicate padding) — and mixing
leaves from one with the other's combiner silently yields a wrong root that
still looks like a root. P1 needs no tree, which avoids the question entirely:
a mutation history is *ordered*, so the head digest already commits to the whole
prefix by induction, and no inclusion proof over an unordered set is required.

So the preimage is plain ``hashlib.sha256`` over the canonical JSON of the
record — the same construction the shipped hash-chained audit log
(``audit/_log.py``) already uses, chosen for consistency with it rather than
inventing a third scheme. ``tests/memstore/test_ledger.py`` asserts the digest
is bit-identical to raw ``hashlib.sha256`` over the documented preimage, so a
tree cannot creep in here later unnoticed.

Unlike ``audit/_log.py`` a record does **not** carry its own hash as a field.
The audit log stores ``entry_hash`` and must strip it before hashing — a
self-referential field every future writer has to remember to exclude. Here the
record's digest is a pure function of the record, recomputed by the verifier
from what it can see; there is no stored value for a tamperer to make agree.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

FACET_NAME = "memstore_mutation"
SCHEMA_VERSION = "0.1.0"

#: The one digest form the rest of the capsule uses. Matched strictly
#: (lower-case hex, exact length) so a truncated or upper-cased digest fails at
#: construction rather than failing to match years later, during an audit of a
#: store whose history nobody present still remembers writing.
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

#: An identifier, never a document. Anything longer is overwhelmingly likely to
#: be store content smuggled through an id field — exactly what I-2 exists to
#: stop. Namespaces and entry ids in real stores are short.
MAX_ID_LENGTH = 512

#: The mutation vocabulary, fixed by ADR-0171 D1. Closed on purpose: an
#: unrecognised op would be a mutation whose *meaning* no later verifier can
#: reconstruct, which is worse than a rejected write at capture time.
MutationOp = Literal["create", "update", "delete", "merge", "supersede", "restore"]

#: Ops after which the entry has no value. ``delete`` is the only one in this
#: slice; kept as a set so a future tombstoning op joins it in one place.
_VALUELESS_OPS: frozenset[str] = frozenset({"delete"})


# ── Errors ────────────────────────────────────────────────────────────────


class MemstoreError(Exception):
    """Base class for every error this layer raises.

    Subclasses :class:`Exception` rather than :class:`ValueError`: a caller
    wrapping store-evidence work wants to catch *memstore* failures, and
    inheriting from ValueError would also swallow unrelated coercion errors
    raised from inside the same block.
    """


class InvalidDigestError(MemstoreError):
    """Raised when a digest or identifier is malformed."""


class ContentCaptureError(MemstoreError):
    """Raised when store content arrives where a digest or id belongs.

    Distinct from :class:`InvalidDigestError` on purpose: a malformed digest is
    a caller mistake, while an entry value or embedding vector reaching this
    boundary is an I-2 violation, and the two want different fixes from whoever
    reads the traceback.
    """


class LedgerRewriteError(MemstoreError):
    """Raised when an operation would rewrite, reorder, or truncate the ledger.

    The load-bearing I-4 guard. The ledger is only evidence because it is
    append-only: if a later write could drop or edit an earlier record, it would
    record the store's *current* state rather than its *history*, and an entry
    that was poisoned and then quietly corrected would become indistinguishable
    from one that was always clean.
    """


# ── Identifier and digest validation ──────────────────────────────────────


def _reject_content(value: object, *, field: str) -> None:
    """Raise if *value* is store content rather than an identifier (I-2)."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        raise ContentCaptureError(
            f"{field} must be a reference (sha256 digest or id), not raw bytes; "
            "digest the value yourself with digest_value() and pass the result "
            "(ADR-0171 I-2 — the ledger never holds store content)"
        )
    if isinstance(value, (list, tuple)):
        # An embedding vector is the one payload shape most likely to arrive
        # here by accident: it is "just numbers", it does not look like content,
        # and it reconstructs the entry well enough to matter (ADR-0021 §4).
        raise ContentCaptureError(
            f"{field} must be a single reference, not a sequence; a vector or "
            "embedding never enters the ledger (ADR-0171 I-2)"
        )


def _validate_digest(value: object, *, field: str) -> str:
    """Return *value* if it is a ``sha256:`` content digest.

    A value binding must be by *content identity*. A URI would not be a binding
    at all — it names where something lives, not what it is, and a store entry
    is precisely the kind of thing that gets replaced underneath its own id.
    """
    _reject_content(value, field=field)
    if not isinstance(value, str):
        raise InvalidDigestError(f"{field} must be a string digest")
    if not _DIGEST_RE.match(value):
        raise InvalidDigestError(f"{field} must be 'sha256:<64 hex>', got {value!r}")
    return value


def _validate_id(value: object, *, field: str) -> str:
    """Return *value* if it is an acceptable short identifier.

    Deliberately permissive beyond digest/URI shapes: agent, run, and entry
    identifiers in the wild are ULIDs, Kubernetes pod names, Slurm job ids and
    vendor-specific keys. Forcing them into a URI would push callers to
    synthesise fake URIs, which is worse evidence than the opaque id they
    actually hold.
    """
    _reject_content(value, field=field)
    if not isinstance(value, str):
        raise InvalidDigestError(f"{field} must be a string identifier")
    if not value.strip():
        raise InvalidDigestError(
            f"{field} must be non-empty; an unnamed store, namespace, or entry "
            "cannot be reconciled against the store later (NF-391)"
        )
    if len(value) > MAX_ID_LENGTH:
        raise ContentCaptureError(
            f"{field} is {len(value)} chars, over the {MAX_ID_LENGTH}-char "
            "identifier limit; this looks like inlined entry content, not an id"
        )
    return value


def digest_value(content: str | bytes) -> str:
    """Return the ``sha256:`` digest of a store entry's value.

    Plain ``hashlib.sha256`` over the value's bytes — no Merkle leaf prefix, no
    tree, no domain separation (see the module docstring for why). Emitted in
    the same ``sha256:<hex>`` form the rest of the capsule uses, so a verifier
    does not have to know which subsystem wrote it.

    The value is hashed and discarded; nothing here retains it (I-2).
    """
    raw = content.encode("utf-8") if isinstance(content, str) else content
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


# ── Objects ───────────────────────────────────────────────────────────────


class MutationActor(BaseModel):
    """*Who* made a mutation — ADR-0171 D1 ``by``.

    ``agent`` is an agent *identifier*, never a person's name, e-mail, or
    credential (ADR-0009). ``run`` is optional: a mutation made by a scheduled
    compaction job, or by a human operator through the store's own console, has
    no NovaFabric run, and inventing one would fabricate provenance.
    """

    model_config = ConfigDict(extra="allow")

    agent: str
    run: str | None = None

    @field_validator("agent", mode="before")
    @classmethod
    def _check_agent(cls, v: object) -> str:
        return _validate_id(v, field="by.agent")

    @field_validator("run", mode="before")
    @classmethod
    def _check_run(cls, v: object) -> str | None:
        if v is None:
            return None
        return _validate_id(v, field="by.run")


class MutationRecord(BaseModel):
    """One link in the KB-mutation chain — who / what / when (NF-391).

    ``prev_record_hash`` is the digest of the immediately preceding record, or
    ``None`` for the genesis record.

    ``None`` rather than a fixed genesis sentinel (``sha256:00…``, or the digest
    of the empty string) on purpose: a sentinel is a value that looks like a
    real predecessor digest to any tool that does not know the constant, so a
    ledger whose true first record was dropped, and whose new first record had
    its link overwritten with the sentinel, would read as a legitimate genesis.
    Absent is not a value (I-4).
    """

    model_config = ConfigDict(extra="allow")

    schema_version: str = SCHEMA_VERSION
    store_id: str
    namespace: str
    entry_id: str
    op: MutationOp
    #: The entry's digest *after* the op. None for ops that leave no value.
    value_digest: str | None = None
    #: The entry's digest *before* the op. None for a `create` — an entry that
    #: did not exist has no prior digest, which is a different fact from one
    #: whose prior digest simply was not recorded.
    prev_value_digest: str | None = None
    by: MutationActor
    at: str
    prev_record_hash: str | None = None

    @field_validator("store_id", "namespace", "entry_id", mode="before")
    @classmethod
    def _check_ids(cls, v: object) -> str:
        # mode="before" on every field: Pydantic's own str coercion would
        # otherwise reject bytes first, with a generic "not a valid string"
        # that tells the caller nothing about *why* bytes are forbidden (I-2).
        return _validate_id(v, field="identifier")

    @field_validator("value_digest", "prev_value_digest", mode="before")
    @classmethod
    def _check_value_digests(cls, v: object) -> str | None:
        if v is None:
            return None
        return _validate_digest(v, field="value_digest")

    @field_validator("prev_record_hash", mode="before")
    @classmethod
    def _check_prev_record_hash(cls, v: object) -> str | None:
        if v is None:
            return None
        return _validate_digest(v, field="prev_record_hash")

    @field_validator("at")
    @classmethod
    def _check_at(cls, v: str) -> str:
        if not v.strip():
            raise ValueError(
                "at must be non-empty; a mutation with no time answers only two "
                "of the three questions the ledger exists to answer (NF-391)"
            )
        return v


class LedgerVerification(BaseModel):
    """The result of a chain walk — *where* it broke, not just *that* it did.

    ``broken_at`` is the index of the first record that fails to verify. A
    verifier that reports only "invalid" is nearly useless to an auditor holding
    a ten-thousand-record store history: the position is what turns a finding
    into an investigation.
    """

    model_config = ConfigDict(extra="allow")

    ok: bool
    broken_at: int | None = None
    reason: str | None = None


class MemstoreMutationFacet(BaseModel):
    """The optional ``facets.memstore_mutation`` block (I-3).

    A per-capsule *reference* to the store-scoped ledger, plus the records this
    run contributed. The durable history is the sealed ledger sidecar, not this
    facet — a capsule carries the slice of the store's history it caused, and
    ``ledger_ref`` binds that slice to the whole.
    """

    model_config = ConfigDict(extra="allow")

    schema_version: str = SCHEMA_VERSION
    store_id: str
    #: Digest of the ledger's head record at the time the facet was written.
    #:
    #: The ADR calls this the "ledger root". It is the **chain head digest**,
    #: not a Merkle root: for an ordered chain the head commits to the entire
    #: prefix by induction, so a tree root would add nothing but the obligation
    #: to choose between the repo's two incompatible Merkle modules. It is what
    #: makes tail truncation detectable — the chain alone cannot see records
    #: removed from its end (see :func:`verify_facet_binding`).
    ledger_ref: str | None = None
    records_in_this_run: list[MutationRecord] = Field(default_factory=list)
    verified: LedgerVerification | None = None

    @field_validator("store_id", mode="before")
    @classmethod
    def _check_store_id(cls, v: object) -> str:
        return _validate_id(v, field="store_id")

    @field_validator("ledger_ref", mode="before")
    @classmethod
    def _check_ledger_ref(cls, v: object) -> str | None:
        if v is None:
            return None
        return _validate_digest(v, field="ledger_ref")


# ── The chain ─────────────────────────────────────────────────────────────


def _canonical_json(payload: dict[str, Any]) -> str:
    """Return the canonical serialisation used as the hash preimage.

    Sorted keys and no whitespace, so the digest is stable across Python dict
    orderings, serialisers, and language runtimes — an offline verifier written
    in something other than Python has to be able to reproduce it. This is the
    same canonicalisation the shipped audit log uses.
    """
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def record_preimage(record: MutationRecord) -> str:
    """Return the exact string :func:`record_digest` hashes.

    Exposed, not private: the preimage *is* the interoperability contract for
    any non-Python verifier, and a contract nobody can inspect is a contract
    nobody can implement.

    ``exclude_none`` is deliberate. An absent ``run`` must hash the same as a
    record written before the field existed; rendering it as ``null`` would make
    the chain digest depend on which optional fields a given NovaFabric version
    happened to know about. It is also what makes a facet round-tripped through
    :func:`attach_facet` — which dumps with the same exclusion — still verify.
    """
    payload: dict[str, Any] = json.loads(record.model_dump_json(exclude_none=True))
    return _canonical_json(payload)


def record_digest(record: MutationRecord) -> str:
    """Return the ``sha256:`` digest of one record — its link in the chain.

    A pure function of the record, *including* its ``prev_record_hash``. That
    inclusion is what makes the chain a chain: altering any record changes its
    digest, which then no longer matches the ``prev_record_hash`` its successor
    carries, and the break is reported at that successor's index.
    """
    return f"sha256:{hashlib.sha256(record_preimage(record).encode()).hexdigest()}"


def chain_head(records: Sequence[MutationRecord]) -> str | None:
    """Return the digest of the last record, or None for an empty ledger.

    None, not the digest of nothing: an empty ledger has no head, and emitting a
    digest for it would give a store with no recorded history a binding
    indistinguishable from one whose history was erased (I-4).
    """
    if not records:
        return None
    return record_digest(records[-1])


def append_mutation(
    records: Sequence[MutationRecord],
    *,
    store_id: str,
    namespace: str,
    entry_id: str,
    op: MutationOp,
    by: MutationActor,
    at: str,
    value_digest: str | None = None,
    prev_value_digest: str | None = None,
) -> list[MutationRecord]:
    """Append one mutation, returning a **new** list; *records* is not mutated.

    The only sanctioned way to grow the ledger. Nothing else in this module
    assigns ``prev_record_hash``, so the chain links correctly because there is
    no other door, not because callers are asked to be careful.

    A ``delete`` carries no ``value_digest`` — a deleted entry has no value, and
    synthesising the digest of the empty string would record that the store now
    holds an empty entry, which is a different fact.

    Raises:
        MemstoreError: if a post-op value digest is supplied for an op that
            leaves no value.
    """
    if op in _VALUELESS_OPS and value_digest is not None:
        raise MemstoreError(
            f"op {op!r} leaves no value but value_digest was supplied; a post-op "
            "digest for a removed entry asserts the store holds something it "
            "does not (ADR-0171 I-1)"
        )
    record = MutationRecord(
        store_id=store_id,
        namespace=namespace,
        entry_id=entry_id,
        op=op,
        value_digest=value_digest,
        prev_value_digest=prev_value_digest,
        by=by,
        at=at,
        prev_record_hash=chain_head(records),
    )
    return [*records, record]


def verify_chain(records: Sequence[MutationRecord]) -> LedgerVerification:
    """Walk the chain offline and report the first break and its index.

    An empty ledger and a single-record ledger both verify: a store whose
    history nobody has recorded yet is not a *broken* history, and reporting it
    as broken would train verifier operators to ignore the finding (I-3 —
    absent is "not recorded", never "tampered").

    Returns, never raises. A chain walk is a *finding*, and a caller sweeping
    several stores must be able to collect findings rather than lose the sweep
    to the first bad one. A CLI would exit non-zero on ``ok=False``
    (`nova memstore mutation verify`, NF-391).
    """
    expected: str | None = None
    for index, record in enumerate(records):
        if record.prev_record_hash != expected:
            if index == 0:
                reason = (
                    "genesis record must have no predecessor, but carries "
                    f"prev_record_hash={record.prev_record_hash!r}; the ledger's "
                    "true first record was dropped or the head was rewritten"
                )
            else:
                reason = (
                    f"prev_record_hash mismatch (stored={record.prev_record_hash!r}, "
                    f"expected={expected!r}); the preceding record was altered, "
                    "replaced, or reordered"
                )
            return LedgerVerification(ok=False, broken_at=index, reason=reason)
        # Advance with the *recomputed* digest, never a stored one, so a
        # tampered record cascades into a break here instead of a successor
        # inheriting the tamperer's value and passing the check.
        expected = record_digest(record)
    return LedgerVerification(ok=True)


def verify_append_only(
    before: Sequence[MutationRecord], after: Sequence[MutationRecord]
) -> None:
    """Assert *after* only appended to *before* (I-4).

    The guard a verifier runs against two versions of the same ledger — the one
    it holds and the one the store's custodian hands back later. Compared
    whole-record, not just on the digests: rewriting a record's ``at`` or its
    ``by.agent`` is the same falsification, and is harder to spot precisely
    because the op and the value digest still read correctly.

    Raises:
        LedgerRewriteError: if the ledger shrank, or if any prior record changed.
    """
    if len(after) < len(before):
        raise LedgerRewriteError(
            f"ledger shrank from {len(before)} to {len(after)} records at index "
            f"{len(after)}; the mutation ledger is append-only (ADR-0171 I-4)"
        )
    for index, (old, new) in enumerate(zip(before, after)):
        if old != new:
            raise LedgerRewriteError(
                f"ledger record {index} was rewritten; a recorded mutation is "
                "evidence of the store's history and is never edited "
                "(ADR-0171 I-4)"
            )


# ── Facet ─────────────────────────────────────────────────────────────────


def build_facet(
    store_id: str,
    records_in_this_run: Iterable[MutationRecord] = (),
    *,
    ledger_ref: str | None = None,
    verified: LedgerVerification | None = None,
) -> MemstoreMutationFacet:
    """Assemble the ``memstore_mutation`` facet.

    ``records_in_this_run`` is **not** sorted, unlike the other facet builders
    in this codebase. A mutation ledger's order *is* the record: sorting by
    timestamp would silently repair a slice whose records arrived out of order,
    hiding exactly the anomaly — a mutation recorded before the one it
    supersedes — that an auditor wants to see. Callers pass records in the order
    things happened; this module preserves that order verbatim.
    """
    return MemstoreMutationFacet(
        store_id=store_id,
        ledger_ref=ledger_ref,
        records_in_this_run=list(records_in_this_run),
        verified=verified,
    )


def facet_for_ledger(
    store_id: str,
    records: Sequence[MutationRecord],
    *,
    records_in_this_run: Sequence[MutationRecord] | None = None,
) -> MemstoreMutationFacet | None:
    """Build a facet bound to *records*, or None when there is nothing to record.

    Returns None for an empty ledger rather than an empty facet: under I-3 a run
    that touched no store must produce no facet at all, and an empty facet would
    assert "this run recorded that it changed nothing" — a claim nobody made.
    """
    if not records:
        return None
    return build_facet(
        store_id,
        records if records_in_this_run is None else records_in_this_run,
        ledger_ref=chain_head(records),
        verified=verify_chain(records),
    )


def attach_facet(
    capsule: dict[str, Any], facet: MemstoreMutationFacet | None
) -> dict[str, Any]:
    """Attach the mutation facet to a capsule dict, additively.

    Writes nothing when *facet* is None: a run with no store material must be
    byte-identical to one captured before this feature existed (I-3). Returns a
    new dict; the input is not mutated.
    """
    if facet is None:
        return capsule
    out = dict(capsule)
    facets = dict(out.get("facets") or {})
    # exclude_none so an absent `run` or `ledger_ref` is *absent*, not `null`.
    # In a layer whose whole point is that "not recorded" differs from "recorded
    # as nothing", that distinction has to survive serialisation — and it is the
    # same exclusion `record_preimage` hashes, so a facet round-tripped through
    # JSON still verifies.
    facets[FACET_NAME] = facet.model_dump(exclude_none=True)
    out["facets"] = facets
    return out


def facet_from_capsule(capsule: dict[str, Any]) -> MemstoreMutationFacet | None:
    """Read the mutation facet back out of a capsule dict.

    Returns None when the capsule has no facet — the overwhelmingly common case,
    and not an error (I-3).
    """
    facets = capsule.get("facets")
    if not isinstance(facets, dict):
        return None
    block = facets.get(FACET_NAME)
    if not isinstance(block, dict):
        return None
    return MemstoreMutationFacet.model_validate(block)


def verify_facet_binding(
    facet: MemstoreMutationFacet, records: Sequence[MutationRecord]
) -> bool:
    """Re-verify the facet's ``ledger_ref`` against the ledger it claims.

    This is the tail-truncation detector. A bare chain cannot see records
    removed from its end — every remaining link still verifies — so the facet's
    separately-sealed head digest is what makes the removal visible.

    Returns False for a facet carrying no ``ledger_ref``, rather than True: an
    unbound facet is precisely the case a binding check exists to surface, and
    returning True would let exactly the facets with nothing to check sail
    through.
    """
    if not facet.ledger_ref:
        return False
    return facet.ledger_ref == chain_head(records)


def scan_for_content(values: Sequence[object]) -> None:
    """Raise if any value is content-shaped rather than reference-shaped (I-2).

    Exposed so a caller assembling records from an untrusted store adapter can
    check before constructing, rather than discovering the problem as a
    validation error halfway through a model.
    """
    for index, value in enumerate(values):
        _validate_id(value, field=f"reference[{index}]")
