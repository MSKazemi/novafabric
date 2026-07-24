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

"""Model-provenance facet — ADR-0152 D1/D2, P1+P2 (NF-201, NF-203, NF-202, NF-206).

Binds the *producer* side of the ML supply chain into the *run*: a capsule that
used a model can carry a reference to the NF-055 model-signing manifest, the
NF-057 SLSA-for-ML attestation, and the NF-058 dataset provenance cards that
model derives from — by digest, so the producer artifact stays the single
source of truth and the capsule stores no copy of it.

Four invariants from ADR-0152 / the NF-201-210 spec §3 shape every choice here:

- **I-1 Additive-first.** The facet lives in optional ``facets.model_provenance``.
  A capsule with no provenance material stays exactly as valid as it was before
  this feature existed, byte for byte.
- **I-2 No payloads.** Only references, digests, versions and counts. Training
  data, model weights and preference labels never enter the capsule through
  this path (ADR-0021 §4, ADR-0009). Passing raw bytes to a reference field is
  an error, not something to silently hash.
- **I-3 Fail-open.** A reference that does not resolve is recorded as
  ``unbound``, never raised and never fatal to the capsule. Absent material
  means no facet — not an empty one.
- **I-4 Records claims, does not adjudicate them.** The facet says which
  artifacts the run *claims* the model is made of. It never asserts the
  training was safe, lawful, or unpoisoned, and it never marks a signature
  ``ok`` that this module did not check.

P1 shipped the facet and the reference binding. **P2 adds** the checkpoint
ancestry chain (NF-202) and the weight-fingerprint pin (NF-206), purely
additively — the facet's ``extra="allow"`` config is what let them land without
a schema break, and a P1-era facet parses unchanged. The deltas & markers
(NF-204/205/209), RLHF provenance (NF-207), card reconciliation (NF-208) and
compute reference (NF-210) remain P3-P5 and are deliberately absent.

Hash construction — a CHAIN, not a tree (P2)
--------------------------------------------
The checkpoint ancestry is a *chain*: each hop names its predecessor by that
predecessor's ``checkpoint_digest``. It is deliberately **not** a Merkle tree,
and this module computes none.

The repository carries two mutually incompatible Merkle constructions —
``evidence/merkle.py`` (RFC 6962, domain-separated leaf/inner prefixes) and
``trust/novaseal/merkle.py`` (pairwise with odd-duplicate padding) — and mixing
leaves from one with the other's combiner silently yields a wrong root that
still looks like a root. P2 needs no tree, which avoids the question entirely:
ancestry is *ordered*, so a running head digest commits to the whole prefix by
induction, and no inclusion proof over an unordered set is required.

So :func:`chain_head` is plain ``hashlib.sha256`` over the canonical JSON of
each hop folded with the running head — the same construction the shipped
hash-chained audit log (``audit/_log.py``) and the KB-mutation ledger
(``memstore/ledger.py``) already use, chosen for consistency with them rather
than inventing a third scheme. The P2 test file asserts the head is bit-identical
to raw ``hashlib.sha256`` over the documented preimage, so a tree cannot creep
in here later unnoticed.

Note the load-bearing limit, the same one ``memstore/ledger.py`` documents: a
bare chain **cannot detect tail truncation**. Drop the last hop and every
remaining link still resolves. The separately-sealed ``checkpoint_chain_head``
is the only detector, and :func:`verify_chain_binding` is where it is checked —
a green :func:`verify_chain` walk is *not* evidence that nothing was dropped.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

FACET_NAME = "model_provenance"
SCHEMA_VERSION = "0.1.0"

#: The one digest form the rest of the capsule uses. Matched strictly (lower-case
#: hex, exact length) so a truncated or upper-cased digest fails loudly here
#: rather than failing to match at verify time, months later, in an audit.
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

#: A producer artifact may also be named by locator instead of by content —
#: an OCI ref, an https URL, a file path published by the build. We accept a
#: URI *shape* only; nothing here dereferences it (I-3: offline, no network).
_URI_RE = re.compile(r"^[a-z][a-z0-9+.\-]*://\S+$")

#: A reference is an identifier, never a document. Anything longer than this is
#: overwhelmingly likely to be inlined content someone tried to smuggle through
#: a "ref" field, which is exactly what I-2 exists to stop.
MAX_REF_LENGTH = 2048

#: Upper bound on hops in one checkpoint chain. Every traversal below is bounded
#: by this, so a hostile or corrupt facet cannot make an offline verifier — which
#: walks capsules it did not produce — loop or allocate without limit. Generous
#: relative to real ancestries (base → continued_pretrain → sft → rlhf →
#: distilled is five hops); the cap is a safety rail, not a modelling statement.
MAX_CHAIN_HOPS = 10_000

#: The seven training stages fixed by ADR-0152 D2 / spec requirement 6. Closed on
#: purpose: an unrecognised stage is a hop whose *meaning* no later verifier can
#: reconstruct, which is worse than a rejected write at capture time.
CheckpointStage = Literal[
    "base",
    "continued_pretrain",
    "sft",
    "rlhf",
    "dpo",
    "distilled",
    "merged",
]

#: The three fingerprint schemes fixed by spec requirement 10. ``merkle_tree`` is
#: a *producer-declared label* describing how the producer derived its digest —
#: NovaFabric records the name and never computes such a tree itself (see the
#: module docstring on why this module builds no Merkle tree at all).
FingerprintScheme = Literal[
    "model_signing_manifest",
    "representation_declared",
    "merkle_tree",
]

#: The outcome of comparing a replay's observed model identity against the pin.
#: ``unpinned`` is a distinct third state, not a flavour of ``match``: a facet
#: that pinned nothing has nothing to agree with, and collapsing it into
#: ``match`` would report a check that never happened (I-4).
FingerprintStatus = Literal["match", "mismatch", "unpinned"]


# ── Errors ────────────────────────────────────────────────────────────────


class InvalidReferenceError(ValueError):
    """Raised when a producer-artifact reference is neither a digest nor a URI."""


class PayloadCaptureError(ValueError):
    """Raised when something artifact-shaped is passed where a reference belongs.

    Distinct from :class:`InvalidReferenceError` on purpose: a malformed digest
    is a caller mistake, while a payload arriving at this boundary is an I-2
    violation, and the two want different fixes from whoever reads the traceback.
    """


class ModelProvenanceError(Exception):
    """Base class for every error the P2 checkpoint/fingerprint layer raises.

    Subclasses :class:`Exception`, not :class:`ValueError`: a malformed ancestry
    is a structural evidence problem a caller should handle by name, and catching
    ``ValueError`` around a facet build would also swallow unrelated coercion
    failures from anywhere in the call stack.

    The two P1 errors above stayed on :class:`ValueError` deliberately rather
    than being re-parented under this class. Callers that already catch
    ``ValueError`` around a P1 facet build are load-bearing, and silently
    changing which exceptions escape a shipped function is a behaviour break that
    no test in this repo would have caught. New code catches by name.
    """


class CheckpointChainError(ModelProvenanceError):
    """Base class for defects in a declared checkpoint ancestry (NF-202)."""


class DuplicateCheckpointError(CheckpointChainError):
    """Raised when two hops share a ``checkpoint_digest``.

    Parents resolve *by digest*, so two distinct hops carrying the same digest
    make every edge pointing at them ambiguous. Rather than pick one, this is
    rejected: an ambiguous ancestry silently resolved is worse evidence than no
    ancestry at all.
    """


class UnresolvedParentError(CheckpointChainError):
    """Raised when a hop's parent digest names no hop in the chain.

    Carries ``checkpoint_id`` and ``parent`` so the caller can name the break.
    The alternative — dropping the dangling edge — would render an *incomplete*
    ancestry indistinguishable from a complete one, which is the single most
    misleading thing this module could do (ADR-0152 Consequences: an unresolvable
    ref degrades to a recorded incompleteness, never to a silent success).
    """

    def __init__(self, checkpoint_id: str, parent: str) -> None:
        self.checkpoint_id = checkpoint_id
        self.parent = parent
        super().__init__(
            f"hop {checkpoint_id!r} names parent {parent!r}, which is not a hop in "
            "this chain; an unresolved parent is recorded or raised, never dropped"
        )


class CyclicCheckpointChainError(CheckpointChainError):
    """Raised when the declared ancestry contains a cycle.

    Carries ``cycle`` — the checkpoint digests on the cycle, in walk order, with
    the entry hop repeated at the end — because "this chain has a cycle" is not
    actionable and "sft-v3 → rlhf-v3 → sft-v3" is. A checkpoint that descends
    from its own descendant is not an ancestry; ADR-0152 D2 requires monotonic,
    acyclic ancestry.
    """

    def __init__(self, cycle: Sequence[str]) -> None:
        self.cycle = list(cycle)
        super().__init__("cyclic checkpoint ancestry: " + " -> ".join(self.cycle))


class ChainTooLargeError(CheckpointChainError):
    """Raised when a chain declares more than :data:`MAX_CHAIN_HOPS` hops."""


class WeightCaptureError(ModelProvenanceError):
    """Raised when model weights arrive where a fingerprint digest belongs.

    The NF-206 boundary, and the reason this module offers no ``digest_weights``
    helper. Hashing a weights blob for the caller would make it effortless to
    hand this module a checkpoint and have it quietly do the right-looking thing;
    the one time it did the wrong thing, nobody would know the weights had been
    in the process at all. The producer digests its own weights (I-2, spec
    requirement 10: "capture of raw weights is prohibited").
    """


# ── Reference validation ──────────────────────────────────────────────────


def _validate_ref(value: object, *, field: str) -> str:
    """Return ``value`` if it is an acceptable producer-artifact reference.

    Raises:
        PayloadCaptureError: if ``value`` is bytes, or is too long to be an
            identifier. Bytes are rejected rather than hashed for the caller:
            hashing here would make it effortless to hand this module a weights
            blob and have it quietly do the right-looking thing, and the one
            time it did the wrong thing nobody would know the bytes had been in
            the process at all.
        InvalidReferenceError: if ``value`` is neither ``sha256:<hex>`` nor a URI.
    """
    if isinstance(value, (bytes, bytearray, memoryview)):
        raise PayloadCaptureError(
            f"{field} must be a reference (sha256 digest or URI), not raw bytes; "
            "digest the artifact yourself with digest_ref() and pass the result "
            "(ADR-0152 I-2 — the capsule never holds weights or training data)"
        )
    if not isinstance(value, str):
        raise InvalidReferenceError(f"{field} must be a string reference")
    if len(value) > MAX_REF_LENGTH:
        raise PayloadCaptureError(
            f"{field} is {len(value)} chars, over the {MAX_REF_LENGTH}-char "
            "reference limit; this looks like inlined artifact content, not a ref"
        )
    if not (_DIGEST_RE.match(value) or _URI_RE.match(value)):
        raise InvalidReferenceError(
            f"{field} must be 'sha256:<64 hex>' or a URI, got {value!r}"
        )
    return value


def _validate_digest(value: object, *, field: str) -> str:
    """Return ``value`` if it is a ``sha256:`` digest.

    Stricter than :func:`_validate_ref`: some fields bind by *content identity*
    and a URI would not be a binding at all — it names where something lives,
    not what it is, and the thing it names can change underneath the capsule.
    """
    ref = _validate_ref(value, field=field)
    if not _DIGEST_RE.match(ref):
        raise InvalidReferenceError(
            f"{field} must be a content digest 'sha256:<64 hex>', not a locator; "
            f"got {ref!r}"
        )
    return ref


def _validate_identifier(value: object, *, field: str) -> str:
    """Return ``value`` if it is a short, non-empty producer-chosen label.

    Used for ``checkpoint_id``, which names a checkpoint the way its producer
    names it — ``base-8b``, ``sft-v3``, a run id, a git sha. Deliberately more
    permissive than :func:`_validate_ref`: forcing these into a digest or URI
    shape would push callers to synthesise fake ones, which is worse evidence
    than the opaque label they actually hold. Length-capped for the same I-2
    reason as everything else here — an "id" field is an easy place to paste a
    manifest.
    """
    if isinstance(value, (bytes, bytearray, memoryview)):
        raise PayloadCaptureError(f"{field} must be an identifier, not raw bytes")
    if not isinstance(value, str) or not value.strip():
        raise InvalidReferenceError(f"{field} must be a non-empty string identifier")
    if len(value) > MAX_REF_LENGTH:
        raise PayloadCaptureError(
            f"{field} is {len(value)} chars, over the {MAX_REF_LENGTH}-char limit; "
            "this looks like inlined artifact content, not an identifier"
        )
    return value


def digest_ref(artifact: str | bytes) -> str:
    """Return the ``sha256:`` reference for a producer artifact.

    Callers hash the artifact — a signing manifest, an SLSA attestation, a
    dataset card — and put only the result in the capsule. Emitted in the same
    ``sha256:<hex>`` form the rest of the capsule uses, so a verifier does not
    have to know which subsystem wrote it.
    """
    raw = artifact.encode("utf-8") if isinstance(artifact, str) else artifact
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


# ── Objects ───────────────────────────────────────────────────────────────


class DataCardRef(BaseModel):
    """A binding to one NF-058 dataset provenance card (NF-203).

    The card itself is *not* re-emitted into the capsule — ADR-0152 D3: the
    producer artifact stays authoritative and the capsule holds the reference.
    """

    model_config = ConfigDict(extra="allow", protected_namespaces=())

    card_digest: str
    dataset_version: str
    #: True when the card digest did not resolve where the caller looked.
    #: Recorded, never fatal (I-3): a capsule that cannot find a card is still
    #: better evidence than a capsule that never said which card it wanted.
    unbound: bool = False

    # mode="before" on every reference field: Pydantic's own str coercion would
    # otherwise reject bytes first, with a generic "not a valid string" that
    # tells the caller nothing about *why* bytes are forbidden here (I-2).
    @field_validator("card_digest", mode="before")
    @classmethod
    def _check_card_digest(cls, v: object) -> str:
        return _validate_digest(v, field="card_digest")

    @field_validator("dataset_version")
    @classmethod
    def _check_dataset_version(cls, v: str) -> str:
        if not v.strip():
            raise ValueError(
                "dataset_version must be non-empty; an unversioned card ref "
                "cannot be reconciled against a checkpoint chain later (NF-203)"
            )
        return v


class CheckpointHop(BaseModel):
    """One hop in the declared checkpoint ancestry (NF-202).

    The checkpoint's *weights* are not stored — only the digest its producer
    published, the stage that produced it, a producer-chosen id, and optionally
    the attestation covering that hop (I-2).
    """

    model_config = ConfigDict(extra="allow", protected_namespaces=())

    stage: CheckpointStage
    checkpoint_id: str
    checkpoint_digest: str
    #: Declared ancestry. ADR-0152 D2 and spec requirement 6 both write ``parent``
    #: as a single digest ("null for base"), but D2's own stage vocabulary
    #: includes ``merged`` — a merge has two or more ancestors by definition, and
    #: a scalar parent cannot express one. The ADR does not settle this, so it is
    #: resolved toward the shape that can represent the stages the ADR itself
    #: lists: a list is accepted as well as a scalar, and the spec's single-string
    #: form stays valid input and output. It is also what makes the acyclicity
    #: check the ADR asks for a real check rather than a formality — in a pure
    #: linked list, "acyclic" is nearly free.
    parent: str | list[str] | None = None
    attestation_ref: str | None = None

    # mode="before" on every reference field: Pydantic's own str coercion would
    # otherwise reject bytes first, with a generic "not a valid string" that
    # tells the caller nothing about *why* bytes are forbidden here (I-2).
    @field_validator("checkpoint_digest", mode="before")
    @classmethod
    def _check_checkpoint_digest(cls, v: object) -> str:
        return _validate_digest(v, field="checkpoint_digest")

    @field_validator("checkpoint_id", mode="before")
    @classmethod
    def _check_checkpoint_id(cls, v: object) -> str:
        return _validate_identifier(v, field="checkpoint_id")

    @field_validator("parent", mode="before")
    @classmethod
    def _check_parent(cls, v: object) -> object:
        if v is None:
            return None
        if isinstance(v, (list, tuple)):
            return [_validate_digest(p, field="parent") for p in v]
        return _validate_digest(v, field="parent")

    @field_validator("attestation_ref", mode="before")
    @classmethod
    def _check_attestation_ref(cls, v: object) -> str | None:
        # A locator is allowed here, unlike `parent`: an attestation_ref points
        # at a producer artifact the verifier may fetch, whereas `parent` *is*
        # the ancestry edge and must bind by content identity.
        if v is None:
            return None
        return _validate_ref(v, field="attestation_ref")

    @property
    def parent_digests(self) -> tuple[str, ...]:
        """Declared parents, normalised to a tuple regardless of wire shape."""
        if self.parent is None:
            return ()
        if isinstance(self.parent, str):
            return (self.parent,)
        return tuple(self.parent)


class WeightFingerprint(BaseModel):
    """The NF-206 pin on the served model's identity.

    A *digest of* the model weights, never the weights. NovaFabric does not
    compute it — the producer does, by whichever ``fingerprint_scheme`` it
    declares — and this module will not accept the bytes it was computed from
    (see :class:`WeightCaptureError`).

    Identity-level only. A matching pin says the run bound the model it said it
    would; it is not a proof the weights were untampered after signing, which
    stays the producer's model-signing responsibility (ADR-0152 Consequences).
    """

    model_config = ConfigDict(extra="allow", protected_namespaces=())

    fingerprint_scheme: FingerprintScheme
    fingerprint_digest: str
    #: Digest of the sealed capsule root this pin is bound into. Optional for the
    #: same reason as the facet's own ``bound_root``: the root is only known once
    #: the capsule is sealed, and a pin made during a run does not have it yet.
    bound_root: str | None = None

    @field_validator("fingerprint_digest", mode="before")
    @classmethod
    def _check_fingerprint_digest(cls, v: object) -> str:
        if isinstance(v, (bytes, bytearray, memoryview)):
            raise WeightCaptureError(
                "fingerprint_digest must be a sha256 digest, not raw bytes; the "
                "producer digests its own weights and publishes the result "
                "(ADR-0152 I-2, NF-206 — the capsule never holds model weights)"
            )
        return _validate_digest(v, field="fingerprint_digest")

    @field_validator("bound_root", mode="before")
    @classmethod
    def _check_pin_bound_root(cls, v: object) -> str | None:
        if v is None:
            return None
        return _validate_digest(v, field="bound_root")


class FingerprintCheck(BaseModel):
    """The outcome of comparing a replay's model identity against the pin (NF-206).

    A *record*, not a resolution. ADR-0152 D2 asks replay to "surface a
    ``fingerprint_mismatch``"; surfacing it means writing down that the replay
    bound a different model, not repairing the discrepancy by re-pinning to what
    was actually observed. Re-pinning would erase the only evidence that the
    served model was not the declared one — which is the exact failure NF-206
    exists to catch (spec §1, inference-service fingerprint spoofing).
    """

    model_config = ConfigDict(extra="allow", protected_namespaces=())

    status: FingerprintStatus
    #: What the facet pinned. None when the facet pinned nothing.
    pinned_digest: str | None = None
    #: What the replay actually bound. None when the replay reported nothing —
    #: which is *also* `unpinned`, because a comparison with one side missing is
    #: not a match.
    observed_digest: str | None = None

    @property
    def mismatch(self) -> bool:
        """True only for a genuine disagreement between two present digests.

        Deliberately not ``status != "match"``: ``unpinned`` is unknown, and
        reporting unknown as a mismatch would flood a fleet-wide sweep with
        findings about capsules that simply never pinned anything, training
        operators to ignore the real ones.
        """
        return self.status == "mismatch"


class VerificationFlags(BaseModel):
    """What a verifier actually checked (I-4).

    Every flag defaults to ``None``, meaning *not checked* — distinct from
    ``False``, meaning *checked and failed*. P1 performs no signature
    verification, so ``signature_ok`` stays ``None`` here; defaulting it to
    ``True`` would launder an unperformed check into the sealed record, and
    defaulting it to ``False`` would slander a signature nobody looked at.
    """

    model_config = ConfigDict(extra="allow", protected_namespaces=())

    refs_resolvable: bool | None = None
    signature_ok: bool | None = None
    sealed_into_root: bool | None = None
    # P2 (NF-202/206), spec §4.2's verify block. Added to the existing flags
    # object rather than a second one: the spec writes a single `verified` block
    # per facet, and a verifier reading two would have to know which check lived
    # where. All still default to None — a P1-era facet that never walked a chain
    # must not read as "walked, and fine".
    chain_walk_ok: bool | None = None
    acyclic: bool | None = None
    no_broken_parent: bool | None = None
    fingerprint_pinned: bool | None = None


class ModelProvenanceFacet(BaseModel):
    """The optional ``facets.model_provenance`` block (NF-201, I-1)."""

    # `protected_namespaces=()` because the ADR's field names — model_id,
    # model_signing_ref — are the wire names in the spec's facet shape. Renaming
    # them to dodge Pydantic's `model_` warning would fork the schema from the
    # ADR for a purely cosmetic reason.
    model_config = ConfigDict(extra="allow", protected_namespaces=())

    schema_version: str = SCHEMA_VERSION
    model_id: str
    model_signing_ref: str | None = None
    slsa_provenance_ref: str | None = None
    data_card_refs: list[DataCardRef] = Field(default_factory=list)
    #: The NF-202 ancestry, in :func:`build_chain` order.
    #:
    #: ``None``, not ``[]``, when no ancestry was recorded — and the difference is
    #: load-bearing. An empty list asserts "this model descends from nothing",
    #: which is a claim; absence says "no ancestry was recorded", which is the
    #: truth in every capsule written before P2 existed. Under ``exclude_none``
    #: the key then vanishes entirely, so a P1-era facet round-trips byte-for-byte
    #: (I-1) — a default of ``[]`` silently added a `checkpoint_chain: []` key to
    #: every existing facet, which the P1 golden-fixture test caught.
    checkpoint_chain: list[CheckpointHop] | None = None
    #: Running head digest over the whole chain — the tail-truncation detector.
    #:
    #: Not a field either ADR-0152 or the NF-201-210 spec names; added here
    #: because without it the chain has a hole. Every hop links to its parent, so
    #: removing hops from the *end* leaves a chain in which every remaining link
    #: still resolves and every check in :func:`verify_chain` still passes. The
    #: head digest is computed over the whole chain and sealed alongside it, so a
    #: verifier holding the head can see that the chain it was handed is shorter
    #: than the one that was sealed (:func:`verify_chain_binding`). Same reasoning
    #: and same construction as ``memstore/ledger.py``'s ``ledger_ref``.
    checkpoint_chain_head: str | None = None
    #: The NF-206 pin on the served model's identity.
    weight_fingerprint: WeightFingerprint | None = None
    #: A recorded replay comparison against that pin. Absent means no replay has
    #: reported back — which is *not* a match (I-4).
    fingerprint_check: FingerprintCheck | None = None
    #: Digest of the sealed capsule root this facet is bound into. Optional in
    #: P1: the root is only known once the capsule is sealed, and a facet built
    #: during a run legitimately does not have it yet.
    bound_root: str | None = None
    verified: VerificationFlags | None = None

    @field_validator("checkpoint_chain_head", mode="before")
    @classmethod
    def _check_chain_head(cls, v: object) -> str | None:
        if v is None:
            return None
        return _validate_digest(v, field="checkpoint_chain_head")

    @field_validator("model_signing_ref", "slsa_provenance_ref", mode="before")
    @classmethod
    def _check_producer_ref(cls, v: object, info: Any) -> str | None:
        if v is None:
            return None
        return _validate_ref(v, field=str(info.field_name))

    @field_validator("bound_root", mode="before")
    @classmethod
    def _check_bound_root(cls, v: object) -> str | None:
        if v is None:
            return None
        return _validate_digest(v, field="bound_root")

    @property
    def has_material(self) -> bool:
        """True when the facet carries provenance the capsule did not already have.

        ``model_id`` alone does not count. The capsule has recorded the model as
        an opaque id since v0.2; a facet repeating it would add a block, a
        schema version and a seal surface while answering none of the questions
        ADR-0152 exists to answer. Only a producer-artifact binding counts as
        material.
        """
        return bool(
            self.model_signing_ref
            or self.slsa_provenance_ref
            or self.data_card_refs
            # P2: a chain or a pin is provenance material in its own right. A run
            # that knows only which checkpoints its model descends from still
            # answers more than `model_id` alone ever did.
            or self.checkpoint_chain
            or self.weight_fingerprint
        )


# ── Checkpoint chain (NF-202) ─────────────────────────────────────────────


def _index_hops(hops: Sequence[CheckpointHop]) -> dict[str, CheckpointHop]:
    """Index hops by checkpoint digest, rejecting duplicates."""
    if len(hops) > MAX_CHAIN_HOPS:
        raise ChainTooLargeError(
            f"{len(hops)} hops exceeds the {MAX_CHAIN_HOPS}-hop limit; an offline "
            "verifier must be able to walk this chain in bounded work"
        )
    index: dict[str, CheckpointHop] = {}
    for hop in hops:
        if hop.checkpoint_digest in index:
            raise DuplicateCheckpointError(
                f"two hops share checkpoint_digest {hop.checkpoint_digest!r} "
                f"({index[hop.checkpoint_digest].checkpoint_id!r} and "
                f"{hop.checkpoint_id!r}); parents resolve by digest, so this "
                "ancestry is ambiguous"
            )
        index[hop.checkpoint_digest] = hop
    return index


def _find_cycle(index: dict[str, CheckpointHop], remaining: Iterable[str]) -> list[str]:
    """Return one concrete cycle path from the hops Kahn's algorithm rejected.

    Walks parent edges iteratively — never recursively, and bounded by the hop
    count — because a corrupt facet is exactly the input that would blow a
    recursive walk's stack, and this code runs inside offline verifiers on
    capsules they did not produce (the repo's bounded-recursion rule).
    """
    start = next(iter(remaining))
    path: list[str] = []
    seen: dict[str, int] = {}
    current = start
    for _ in range(len(index) + 1):
        if current in seen:
            # Trim the lead-in: report the cycle itself, not the walk that
            # happened to reach it.
            return [*path[seen[current] :], current]
        seen[current] = len(path)
        path.append(current)
        parents = [p for p in index[current].parent_digests if p in index]
        if not parents:
            break
        # Any parent still unemitted leads back into the cycle; picking the
        # lexicographically smallest keeps the reported path deterministic.
        current = sorted(parents)[0]
    # Unreachable for a graph Kahn's algorithm left behind (every such hop has at
    # least one resolved parent, so the walk cannot terminate before revisiting),
    # but returning the path is better than raising here.
    return path


def build_chain(hops: Sequence[CheckpointHop]) -> list[CheckpointHop]:
    """Resolve and order the declared ancestry, or raise naming the defect.

    Returns the hops in a **deterministic topological order**: every hop appears
    after all of its declared parents, which is precisely spec requirement 6's
    "each ``parent`` MUST resolve to an *earlier* hop". Ordering here rather than
    trusting the caller makes that invariant hold by construction instead of by
    discipline. Hops with no ancestry relation to each other are broken by
    ``checkpoint_digest``, ascending.

    That tiebreak is a *serialisation* guarantee and nothing more. It must not be
    read as training order, wall-clock order, or precedence; it exists so two
    runs declaring the same ancestry produce the same facet bytes, because the
    facet is hashed into the seal and a stable ordering is what keeps that hash
    comparable.

    This is deliberately the same algorithm and the same shape as
    ``science/provenance.py``'s ``build_dag`` (ADR-0164), which solved this exact
    problem — iterative Kahn's, node cap, unresolved-parents checked first. The
    two are not shared because each facet's node model is owned by its own ADR
    and they evolve independently; the *approach* is shared on purpose so there
    is one answer in this repo to "how do we walk untrusted ancestry safely".

    Raises:
        DuplicateCheckpointError: two hops share a ``checkpoint_digest``.
        UnresolvedParentError: a parent digest names no hop in the chain.
        CyclicCheckpointChainError: the ancestry contains a cycle. A hop that is
            its own parent is a cycle of length one and is rejected here.
        ChainTooLargeError: more than :data:`MAX_CHAIN_HOPS` hops.
    """
    index = _index_hops(hops)

    # Unresolved parents are checked *before* the topological sort, so the error
    # a caller sees names the dangling edge rather than a downstream symptom.
    for hop in hops:
        for parent in hop.parent_digests:
            if parent not in index:
                raise UnresolvedParentError(hop.checkpoint_id, parent)

    # Kahn's algorithm — iterative by construction, so an adversarial depth
    # cannot exhaust the stack.
    indegree = {d: len(index[d].parent_digests) for d in index}
    children: dict[str, list[str]] = {d: [] for d in index}
    for digest, hop in index.items():
        for parent in hop.parent_digests:
            children[parent].append(digest)

    # Sorted frontier + sorted insertion is what makes the output order stable.
    frontier = sorted(d for d, deg in indegree.items() if deg == 0)
    order: list[CheckpointHop] = []
    while frontier:
        digest = frontier.pop(0)
        order.append(index[digest])
        newly_free = []
        for child in children[digest]:
            indegree[child] -= 1
            if indegree[child] == 0:
                newly_free.append(child)
        if newly_free:
            frontier = sorted(frontier + newly_free)

    if len(order) != len(index):
        emitted = {h.checkpoint_digest for h in order}
        raise CyclicCheckpointChainError(
            _find_cycle(index, sorted(set(index) - emitted))
        )
    return order


def verify_chain(hops: Sequence[CheckpointHop]) -> VerificationFlags:
    """Check the ancestry without raising, returning the ADR's verify flags.

    The non-raising counterpart to :func:`build_chain`, for the record-and-report
    path: a verifier walking someone else's capsule wants to *report* a broken
    ancestry, not abort on it (I-3/I-4).

    ``fingerprint_pinned`` and every P1 flag are left ``None`` because this
    function checks the chain and nothing else — see :class:`VerificationFlags`
    on why an unperformed check must not become a flag.

    An empty chain returns all-``None``, not ``chain_walk_ok=True``: there was
    nothing to walk, and "vacuously fine" and "checked and fine" must not
    serialise to the same thing in a sealed record.
    """
    if not hops:
        return VerificationFlags()
    try:
        build_chain(hops)
    except UnresolvedParentError:
        return VerificationFlags(chain_walk_ok=False, no_broken_parent=False)
    except CyclicCheckpointChainError:
        # `no_broken_parent` is True here on purpose: every parent resolved —
        # that is precisely how the walk got far enough to find a cycle.
        return VerificationFlags(
            chain_walk_ok=False, acyclic=False, no_broken_parent=True
        )
    except CheckpointChainError:
        # Duplicate digests / oversize chain: the walk did not complete, but
        # neither acyclicity nor parent resolution was established either way, so
        # both stay `None` rather than being guessed.
        return VerificationFlags(chain_walk_ok=False)
    return VerificationFlags(chain_walk_ok=True, acyclic=True, no_broken_parent=True)


def _canonical_json(payload: dict[str, Any]) -> str:
    """Return the canonical serialisation used as the hash preimage.

    Sorted keys and no whitespace, so the digest is stable across Python dict
    orderings, serialisers, and language runtimes — an offline verifier written
    in something other than Python has to be able to reproduce it. This is the
    same canonicalisation the shipped audit log and mutation ledger use.
    """
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def hop_preimage(hop: CheckpointHop, prev_head: str | None) -> str:
    """Return the exact string :func:`chain_head` hashes for one hop.

    Exposed, not private: the preimage *is* the interoperability contract for any
    non-Python verifier, and a contract nobody can inspect is a contract nobody
    can implement.

    ``exclude_none`` is deliberate. An absent ``attestation_ref`` must hash the
    same as a hop written before the field existed; rendering it as ``null``
    would make the head depend on which optional fields a given NovaFabric
    version happened to know about. It is also the same exclusion
    :func:`attach_facet` dumps with, so a facet round-tripped through JSON still
    verifies.

    ``prev_head`` is omitted entirely for the first hop rather than serialised as
    ``null``, for the reason ``memstore/ledger.py`` gives about genesis
    sentinels: a fixed stand-in for "no predecessor" is a value that looks real
    to any tool not carrying the constant.
    """
    payload: dict[str, Any] = {"hop": json.loads(hop.model_dump_json(exclude_none=True))}
    if prev_head is not None:
        payload["prev_head"] = prev_head
    return _canonical_json(payload)


def chain_head(hops: Sequence[CheckpointHop]) -> str | None:
    """Return the running head digest over the whole chain, or None if empty.

    Plain ``hashlib.sha256`` over :func:`hop_preimage`, folded left. **Not** a
    Merkle root — see the module docstring for why this module computes none, and
    why picking either of the repo's two incompatible tree modules would be a
    hazard with no upside for an ordered structure.

    None for an empty chain, not the digest of nothing: a model with no recorded
    ancestry has no head, and emitting a digest for it would give it a binding
    indistinguishable from one whose ancestry was erased.
    """
    head: str | None = None
    for hop in hops:
        head = f"sha256:{hashlib.sha256(hop_preimage(hop, head).encode()).hexdigest()}"
    return head


# ── Weight-fingerprint pin (NF-206) ───────────────────────────────────────


def pin_fingerprint(
    fingerprint_digest: str,
    *,
    scheme: FingerprintScheme = "model_signing_manifest",
    bound_root: str | None = None,
) -> WeightFingerprint:
    """Pin the served model's identity by a *producer-computed* digest.

    Takes a digest, never weights. There is no ``digest_weights`` counterpart to
    :func:`digest_ref` anywhere in this module, and that absence is the design:
    see :class:`WeightCaptureError`.

    Raises:
        WeightCaptureError: if raw bytes are passed.
        InvalidReferenceError: if the digest is malformed.
    """
    return WeightFingerprint(
        fingerprint_scheme=scheme,
        fingerprint_digest=fingerprint_digest,
        bound_root=bound_root,
    )


def check_fingerprint(
    facet: ModelProvenanceFacet, observed_digest: str | None
) -> FingerprintCheck:
    """Compare a replay's observed model identity against the pin (NF-206).

    Returns a record; it never raises and never touches the facet. The three
    outcomes are kept distinct on purpose:

    - ``unpinned`` — the facet pinned nothing, or the replay reported nothing.
      **Never** ``match``. An absent fingerprint means unknown, and a check that
      could not be performed must not serialise as a check that passed (I-4).
    - ``match`` — both digests present and equal.
    - ``mismatch`` — both present and different. This is what ADR-0152 D2 means
      by surfacing ``fingerprint_mismatch``.

    A malformed ``observed_digest`` is reported as ``unpinned`` rather than
    raised: a replay handing back a garbage identity has told us nothing, which
    is exactly "unknown", and raising on the replay path would let a corrupt
    observation block a workload (I-3).
    """
    pinned = facet.weight_fingerprint.fingerprint_digest if facet.weight_fingerprint else None
    if pinned is None or not observed_digest or not _DIGEST_RE.match(observed_digest):
        return FingerprintCheck(
            status="unpinned",
            pinned_digest=pinned,
            observed_digest=observed_digest if observed_digest else None,
        )
    return FingerprintCheck(
        status="match" if pinned == observed_digest else "mismatch",
        pinned_digest=pinned,
        observed_digest=observed_digest,
    )


def record_fingerprint_check(
    facet: ModelProvenanceFacet, check: FingerprintCheck
) -> ModelProvenanceFacet:
    """Return a **new** facet carrying *check*, with the pin left untouched.

    The whole point of the NF-206 surface: a mismatch is *recorded*, not
    resolved. This function cannot re-pin — it writes only ``fingerprint_check``
    and copies ``weight_fingerprint`` through verbatim — because a "helpful"
    re-pin to the observed digest would destroy the only evidence that the
    replayed binding was not the declared model, which is the exact spoofing case
    NF-206 exists to catch. The input facet is not mutated.
    """
    return facet.model_copy(update={"fingerprint_check": check})


# ── Construction ──────────────────────────────────────────────────────────


def data_card_ref(
    card_digest: str,
    dataset_version: str,
    *,
    resolved: bool = True,
) -> DataCardRef:
    """Bind one NF-058 dataset card by digest + version (NF-203).

    ``resolved=False`` records ``unbound: true`` — the caller looked for the
    card and did not find it. That is a fact worth sealing, so it is recorded
    rather than dropped or raised (I-3).
    """
    return DataCardRef(
        card_digest=card_digest,
        dataset_version=dataset_version,
        unbound=not resolved,
    )


def build_facet(
    model_id: str,
    *,
    model_signing_ref: str | None = None,
    slsa_provenance_ref: str | None = None,
    data_card_refs: Iterable[DataCardRef] = (),
    checkpoint_chain: Iterable[CheckpointHop] = (),
    checkpoint_chain_head: str | None = None,
    weight_fingerprint: WeightFingerprint | None = None,
    fingerprint_check: FingerprintCheck | None = None,
    bound_root: str | None = None,
    verified: VerificationFlags | None = None,
) -> ModelProvenanceFacet:
    """Assemble the model-provenance facet from producer-artifact references.

    Card refs are ordered by ``card_digest`` so two runs binding the same set of
    cards produce the same facet bytes regardless of the order the caller
    discovered them — the facet is hashed into the seal, and a stable ordering
    is what keeps that hash comparable across runs. Checkpoint hops are ordered
    by :func:`build_chain` for the same reason, plus the stronger one that the
    order carries meaning (spec requirement 6's monotonic ancestry).

    ``checkpoint_chain_head`` is computed from the ordered chain when the caller
    does not supply one, so the truncation detector exists by default rather than
    only when someone remembered to ask for it. A caller re-attaching a facet it
    is verifying passes the *sealed* head explicitly, which is what lets
    :func:`verify_chain_binding` see a short chain.

    Raises:
        CheckpointChainError: if the declared ancestry is malformed. A cycle or a
            dangling parent is *caller* input, not runtime absence, and I-3's
            fail-open rule covers missing material — not incoherent material.
            Writing a known-broken ancestry into a sealed capsule would be worse
            than writing none. Use :func:`verify_chain` for the report-only path.
    """
    ordered = build_chain(list(checkpoint_chain))
    return ModelProvenanceFacet(
        model_id=model_id,
        model_signing_ref=model_signing_ref,
        slsa_provenance_ref=slsa_provenance_ref,
        data_card_refs=sorted(data_card_refs, key=lambda r: r.card_digest),
        # `or None` so a caller passing no chain leaves the key absent rather
        # than asserting an empty ancestry — see the field's comment.
        checkpoint_chain=ordered or None,
        checkpoint_chain_head=(
            checkpoint_chain_head
            if checkpoint_chain_head is not None
            else chain_head(ordered)
        ),
        weight_fingerprint=weight_fingerprint,
        fingerprint_check=fingerprint_check,
        bound_root=bound_root,
        verified=verified,
    )


def attach_facet(
    capsule: dict[str, Any], facet: ModelProvenanceFacet
) -> dict[str, Any]:
    """Attach the model-provenance facet to a capsule dict, additively.

    Writes nothing when the facet carries no producer-artifact binding: a run
    whose model has no provenance material must be byte-identical to one
    captured before this feature existed (I-1, I-3). Returns a new dict; the
    input is not mutated.
    """
    if not facet.has_material:
        return capsule
    out = dict(capsule)
    facets = dict(out.get("facets") or {})
    # exclude_none so an unchecked verification flag is *absent*, not `null` —
    # see VerificationFlags: absent means "not checked" and that distinction has
    # to survive serialisation to be worth anything.
    facets[FACET_NAME] = facet.model_dump(exclude_none=True)
    out["facets"] = facets
    return out


# ── Verification ──────────────────────────────────────────────────────────


def verify_artifact_ref(ref: str | None, artifact: str | bytes) -> bool:
    """Re-verify a reference against the producer artifact it names.

    Returns False for an absent ref, and False for a URI ref: neither one binds
    the artifact's content, and returning True would let exactly the references
    with nothing to check pass a binding check (the ADR-0145 rule, same reason).
    A URI is verified by resolving it, which this module deliberately does not
    do — P1 is offline.
    """
    if not ref or not _DIGEST_RE.match(ref):
        return False
    return ref == digest_ref(artifact)


def verify_chain_binding(
    facet: ModelProvenanceFacet, hops: Sequence[CheckpointHop] | None = None
) -> bool:
    """Re-verify the facet's ``checkpoint_chain_head`` against the chain (NF-202).

    **This is the tail-truncation detector, and the only one.** A bare chain
    cannot see hops removed from its end — every remaining parent link still
    resolves and :func:`verify_chain` still returns ``chain_walk_ok=True`` — so
    the separately-sealed head digest is what makes the removal visible. Reading
    a green chain walk as proof that nothing was dropped is the mistake this
    function exists to prevent.

    *hops* defaults to the facet's own chain, which checks internal consistency.
    Pass the chain from another source (a sidecar, a peer's copy) to check that
    one against this facet's sealed head.

    Returns False for a facet carrying no head, rather than True: an unbound
    facet is precisely the case a binding check exists to surface, and returning
    True would let exactly the facets with nothing to check sail through.
    """
    if not facet.checkpoint_chain_head:
        return False
    return facet.checkpoint_chain_head == chain_head(
        (facet.checkpoint_chain or []) if hops is None else hops
    )


def unbound_card_refs(facet: ModelProvenanceFacet) -> list[DataCardRef]:
    """Return the card refs that did not resolve.

    The degradation ADR-0152 names in its Consequences: an unresolvable card
    ref is recorded, surfaced, and not treated as a guarantee — the caller
    decides what to do about it, because NovaFabric does not adjudicate (I-4).
    """
    return [ref for ref in facet.data_card_refs if ref.unbound]


def facet_from_capsule(capsule: dict[str, Any]) -> ModelProvenanceFacet | None:
    """Read the model-provenance facet back out of a capsule dict.

    Returns None when the capsule has no facet — the overwhelmingly common
    case, and not an error (I-3).
    """
    facets = capsule.get("facets")
    if not isinstance(facets, dict):
        return None
    block = facets.get(FACET_NAME)
    if not isinstance(block, dict):
        return None
    return ModelProvenanceFacet.model_validate(block)


def scan_for_payloads(values: Sequence[object]) -> None:
    """Raise if any value is artifact-shaped rather than reference-shaped (I-2).

    The spec's normative requirement 2 wants a validator that *rejects* a
    weights-blob / raw-dataset shape rather than one that merely documents the
    rule. This is that validator, exposed so a caller assembling a facet from
    untrusted producer metadata can check before constructing.
    """
    for index, value in enumerate(values):
        _validate_ref(value, field=f"reference[{index}]")
