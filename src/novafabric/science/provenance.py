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

"""Science-provenance facet — ADR-0164 D1/P1 (NF-321).

Binds an agentic-science run's *hypothesis → experiment → result* trail into
the capsule as an offline-walkable DAG of digests: what the agent hypothesised,
what it designed, what it ran, what it observed, what it concluded, and what it
claimed — each node bound by content digest, none of it stored as content.

Four invariants from ADR-0164 / the NF-321-330 spec §3 shape every choice here:

- **I-1 Additive-first.** The facet lives in optional ``facets.science_provenance``.
  A capsule with no science material stays exactly as valid as it was before
  this feature existed, byte for byte.
- **I-2 No payloads.** Hypotheses, protocols, datasets, instrument telemetry and
  manuscript bodies are represented only by ``sha256:`` digests and short
  identifiers. Passing raw content to a digest field is an error, not something
  to silently hash (ADR-0021 §4, ADR-0009).
- **I-3 Fail-open.** No science material means *no facet*, not an empty one, and
  never an exception on the capture path. Population is best-effort.
- **I-4 Records claims, does not adjudicate them.** The DAG is the producer's
  *declared* ancestry. NovaFabric stores and walks it; it never authors a node,
  generates a hypothesis, runs an experiment, or asserts that a result is
  correct, novel, reproducible-in-fact, or true.

P1 is the facet and the DAG only. The reproducibility receipt (NF-323), FAIR
binding (NF-324), lab/instrument provenance (NF-322/329), research-integrity
records (NF-326..330) and claim grounding (NF-325) are P2–P5 and deliberately
absent — the facet's ``extra="allow"`` config is what lets a later slice add
them without a schema break.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

FACET_NAME = "science_provenance"
SCHEMA_VERSION = "0.1.0"

#: The six node kinds of the ADR-0164 D1 lineage. Kept as a closed Literal: an
#: unrecognised kind is either a typo or an unreviewed evidence surface entering
#: the sealed root, and both should fail at build time rather than at audit time.
NodeKind = Literal[
    "hypothesis",
    "experiment_design",
    "experiment_run",
    "observation",
    "result",
    "claim",
]

#: The one digest form the rest of the capsule uses. Matched strictly (lower-case
#: hex, exact length) so a truncated or upper-cased digest fails loudly here
#: rather than failing to resolve at verify time, months later, in an audit.
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

#: An identifier is a label, never a document. Anything longer is overwhelmingly
#: likely to be inlined research content someone tried to smuggle through an
#: "id" or "ref" field, which is exactly what I-2 exists to stop.
MAX_IDENTIFIER_LENGTH = 512

#: Upper bound on nodes in one facet. Every traversal below is bounded by this,
#: so a hostile or corrupt facet cannot make an offline verifier — which walks
#: capsules it did not produce — loop or allocate without limit.
MAX_DAG_NODES = 100_000

# The digest/identifier validators below are deliberately re-stated rather than
# imported from `provenance/model_provenance.py`. That module's `_validate_ref`
# is private to the ADR-0152 facet and accepts locator shapes (OCI refs, build
# URLs) that mean nothing here; sharing it would couple two facet schemas that
# are owned by different ADRs and evolve independently.


# ── Errors ────────────────────────────────────────────────────────────────


class ScienceProvenanceError(Exception):
    """Base class for every error this module raises.

    Subclasses :class:`Exception`, not :class:`ValueError`: a malformed lineage
    is a structural evidence problem a caller should handle by name, and
    catching ``ValueError`` around a facet build would also swallow unrelated
    coercion failures from anywhere in the call stack.
    """


class InvalidNodeDigestError(ScienceProvenanceError):
    """Raised when a node digest is not a ``sha256:<64 hex>`` content digest."""


class PayloadCaptureError(ScienceProvenanceError):
    """Raised when research content arrives where an identifier belongs.

    Distinct from :class:`InvalidNodeDigestError` on purpose: a malformed digest
    is a caller mistake, while a payload reaching this boundary is an I-2
    violation, and the two want different fixes from whoever reads the traceback.
    """


class DuplicateNodeError(ScienceProvenanceError):
    """Raised when two nodes share a ``node_digest``.

    Parents resolve *by digest*, so two distinct nodes carrying the same digest
    make every edge pointing at them ambiguous. Rather than pick one, this is
    rejected: an ambiguous ancestry silently resolved is worse evidence than no
    ancestry at all.
    """


class UnresolvedParentError(ScienceProvenanceError):
    """Raised when a node's parent digest names no node in the facet.

    Carries ``node_id`` and ``parent`` so the caller can name the break. The
    alternative — dropping the dangling edge — would render an *incomplete* DAG
    indistinguishable from a complete one, which is the single most misleading
    thing this module could do (ADR-0164 Consequences: an unresolvable ref
    degrades to a recorded incompleteness, never to a silent success).
    """

    def __init__(self, node_id: str, parent: str) -> None:
        self.node_id = node_id
        self.parent = parent
        super().__init__(
            f"node {node_id!r} names parent {parent!r}, which is not a node in "
            "this facet; an unresolved parent is recorded or raised, never dropped"
        )


class CyclicLineageError(ScienceProvenanceError):
    """Raised when the declared ancestry contains a cycle.

    Carries ``cycle`` — the node digests on the cycle, in walk order, with the
    entry node repeated at the end — because "this DAG has a cycle" is not
    actionable and "H1 → D1 → H1" is. A hypothesis that descends from its own
    conclusion is not a lineage; it is circular reasoning made machine-readable,
    and ADR-0164 D1 requires acyclic ancestry.
    """

    def __init__(self, cycle: Sequence[str]) -> None:
        self.cycle = list(cycle)
        super().__init__("cyclic science lineage: " + " -> ".join(self.cycle))


class DagTooLargeError(ScienceProvenanceError):
    """Raised when a facet declares more than :data:`MAX_DAG_NODES` nodes."""


# ── Validation helpers ────────────────────────────────────────────────────


def _validate_digest(value: object, *, field: str) -> str:
    """Return ``value`` if it is a ``sha256:`` content digest.

    Raises:
        PayloadCaptureError: if ``value`` is bytes, or is too long to be an
            identifier. Bytes are rejected rather than hashed for the caller:
            hashing here would make it effortless to hand this module a raw
            dataset or an unpublished manuscript and have it quietly do the
            right-looking thing, and the one time it did the wrong thing nobody
            would know the bytes had been in the process at all (I-2).
        InvalidNodeDigestError: if ``value`` is not ``sha256:<64 hex>``.
    """
    if isinstance(value, (bytes, bytearray, memoryview)):
        raise PayloadCaptureError(
            f"{field} must be a sha256 digest, not raw bytes; digest the content "
            "yourself with digest_node() and pass the result (ADR-0164 I-2 — the "
            "capsule never holds datasets, protocols, or manuscript bodies)"
        )
    if not isinstance(value, str):
        raise InvalidNodeDigestError(f"{field} must be a string digest")
    if len(value) > MAX_IDENTIFIER_LENGTH:
        raise PayloadCaptureError(
            f"{field} is {len(value)} chars, over the {MAX_IDENTIFIER_LENGTH}-char "
            "limit; this looks like inlined research content, not a digest"
        )
    if not _DIGEST_RE.match(value):
        raise InvalidNodeDigestError(
            f"{field} must be a content digest 'sha256:<64 hex>', got {value!r}"
        )
    return value


def _validate_identifier(value: object, *, field: str) -> str:
    """Return ``value`` if it is a short, non-empty label.

    Used for ``node_id`` and ``agent_run_ref``, which are producer-chosen names
    rather than content digests. Length-capped for the same I-2 reason as
    :func:`_validate_digest`: a "ref" field is the easiest place to paste a
    protocol.
    """
    if isinstance(value, (bytes, bytearray, memoryview)):
        raise PayloadCaptureError(f"{field} must be an identifier, not raw bytes")
    if not isinstance(value, str) or not value.strip():
        raise InvalidNodeDigestError(f"{field} must be a non-empty string identifier")
    if len(value) > MAX_IDENTIFIER_LENGTH:
        raise PayloadCaptureError(
            f"{field} is {len(value)} chars, over the {MAX_IDENTIFIER_LENGTH}-char "
            "limit; this looks like inlined research content, not an identifier"
        )
    return value


def digest_node(content: str | bytes) -> str:
    """Return the ``sha256:`` digest of a lineage node's content.

    Callers hash the hypothesis text, the experiment protocol, the observation
    record — and put only the result in the capsule (I-2). Emitted in the same
    ``sha256:<hex>`` form the rest of the capsule uses, so a verifier does not
    have to know which subsystem wrote it.
    """
    raw = content.encode("utf-8") if isinstance(content, str) else content
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


# ── Objects ───────────────────────────────────────────────────────────────


class ScienceNode(BaseModel):
    """One node in the declared hypothesis→claim lineage (NF-321).

    The node's *content* is not stored — only its digest, its kind, a
    producer-chosen id, and optionally the capsule that produced it (I-2).
    """

    model_config = ConfigDict(extra="allow")

    kind: NodeKind
    node_id: str
    node_digest: str
    #: Declared ancestry. ADR-0164 D1 and the NF-321 spec shape write a single
    #: `parent` digest, but they both call the structure a *DAG* and require
    #: *acyclicity* — properties that are trivial in a chain and only meaningful
    #: when edges converge. Real agentic science converges: two observations
    #: feed one result. So a list is accepted here as well as a scalar, and the
    #: spec's single-string form remains valid input and output. Resolving this
    #: ambiguity toward the DAG reading is what makes the acyclicity check the
    #: ADR asks for a real check rather than a formality.
    parent: str | list[str] | None = None
    agent_run_ref: str | None = None

    # mode="before" on every reference field: Pydantic's own str coercion would
    # otherwise reject bytes first, with a generic "not a valid string" that
    # tells the caller nothing about *why* bytes are forbidden here (I-2).
    @field_validator("node_digest", mode="before")
    @classmethod
    def _check_node_digest(cls, v: object) -> str:
        return _validate_digest(v, field="node_digest")

    @field_validator("node_id", mode="before")
    @classmethod
    def _check_node_id(cls, v: object) -> str:
        return _validate_identifier(v, field="node_id")

    @field_validator("agent_run_ref", mode="before")
    @classmethod
    def _check_agent_run_ref(cls, v: object) -> str | None:
        if v is None:
            return None
        return _validate_identifier(v, field="agent_run_ref")

    @field_validator("parent", mode="before")
    @classmethod
    def _check_parent(cls, v: object) -> object:
        if v is None:
            return None
        if isinstance(v, (list, tuple)):
            return [_validate_digest(p, field="parent") for p in v]
        return _validate_digest(v, field="parent")

    @property
    def parent_digests(self) -> tuple[str, ...]:
        """Declared parents, normalised to a tuple regardless of wire shape."""
        if self.parent is None:
            return ()
        if isinstance(self.parent, str):
            return (self.parent,)
        return tuple(self.parent)


class DagVerification(BaseModel):
    """What a verifier actually checked about the lineage (I-4).

    Every flag defaults to ``None``, meaning *not checked* — distinct from
    ``False``, meaning *checked and failed*. P1 performs no signature or seal
    verification, so ``sealed_into_root`` stays ``None`` unless a caller that
    did the check sets it; defaulting it to ``True`` would launder an unperformed
    check into the sealed record, and defaulting it to ``False`` would slander a
    seal nobody looked at. Absent is not false.
    """

    model_config = ConfigDict(extra="allow")

    dag_walk_ok: bool | None = None
    acyclic: bool | None = None
    no_broken_parent: bool | None = None
    sealed_into_root: bool | None = None


class ScienceProvenanceFacet(BaseModel):
    """The optional ``facets.science_provenance`` block (NF-321, I-1)."""

    model_config = ConfigDict(extra="allow")

    schema_version: str = SCHEMA_VERSION
    hypothesis_experiment_result: list[ScienceNode] = Field(default_factory=list)
    #: Digest of the sealed capsule root this facet is bound into. Optional in
    #: P1: the root is only known once the capsule is sealed, and a facet built
    #: during a run legitimately does not have it yet.
    bound_root: str | None = None
    verified: DagVerification | None = None

    @field_validator("bound_root", mode="before")
    @classmethod
    def _check_bound_root(cls, v: object) -> str | None:
        if v is None:
            return None
        return _validate_digest(v, field="bound_root")

    @property
    def has_material(self) -> bool:
        """True when the facet carries lineage the capsule did not already have."""
        return bool(self.hypothesis_experiment_result)


# ── DAG construction ──────────────────────────────────────────────────────


def _index_nodes(nodes: Sequence[ScienceNode]) -> dict[str, ScienceNode]:
    """Index nodes by digest, rejecting duplicates."""
    if len(nodes) > MAX_DAG_NODES:
        raise DagTooLargeError(
            f"{len(nodes)} nodes exceeds the {MAX_DAG_NODES}-node limit; an "
            "offline verifier must be able to walk this facet in bounded work"
        )
    index: dict[str, ScienceNode] = {}
    for node in nodes:
        if node.node_digest in index:
            raise DuplicateNodeError(
                f"two nodes share node_digest {node.node_digest!r} "
                f"({index[node.node_digest].node_id!r} and {node.node_id!r}); "
                "parents resolve by digest, so this ancestry is ambiguous"
            )
        index[node.node_digest] = node
    return index


def _find_cycle(
    index: dict[str, ScienceNode], remaining: Iterable[str]
) -> list[str]:
    """Return one concrete cycle path from the nodes Kahn's algorithm rejected.

    Walks parent edges iteratively — never recursively, and bounded by the node
    count — because a corrupt facet is exactly the input that would blow a
    recursive walk's stack, and this code runs inside offline verifiers on
    capsules they did not produce.
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
        # Any parent still in `remaining` leads back into a cycle; picking the
        # lexicographically smallest keeps the reported path deterministic.
        current = sorted(parents)[0]
    # Unreachable for a graph Kahn's algorithm left behind (every such node has
    # at least one resolved parent, so the walk cannot terminate before
    # revisiting), but returning the path is better than raising here.
    return path


def build_dag(nodes: Sequence[ScienceNode]) -> list[ScienceNode]:
    """Resolve and order the declared lineage, or raise naming the defect.

    Returns the nodes in a **deterministic topological order**: every node
    appears after all of its declared parents. Independent nodes — those with no
    ancestry relation to each other — are broken by ``node_digest``, ascending.

    That order is a *serialisation* guarantee, not a stronger causal claim than
    the producer already made. The parent edges are the producer's declared
    ancestry and the topological order faithfully reflects them; the lexicographic
    tiebreak between independent nodes means nothing at all and must not be read
    as execution order, wall-clock order, or precedence. It exists so that two
    runs declaring the same lineage produce the same facet bytes — the facet is
    hashed into the seal, and a stable ordering is what keeps that hash
    comparable.

    Raises:
        DuplicateNodeError: two nodes share a ``node_digest``.
        UnresolvedParentError: a parent digest names no node in the facet.
        CyclicLineageError: the declared ancestry contains a cycle. A node that
            is its own parent is a cycle of length one and is rejected here.
        DagTooLargeError: more than :data:`MAX_DAG_NODES` nodes.
    """
    index = _index_nodes(nodes)

    # Unresolved parents are checked *before* the topological sort, so the error
    # a caller sees names the dangling edge rather than a downstream symptom.
    for node in nodes:
        for parent in node.parent_digests:
            if parent not in index:
                raise UnresolvedParentError(node.node_id, parent)

    # Kahn's algorithm — iterative by construction, so an adversarial depth
    # cannot exhaust the stack (the repo's bounded-recursion rule).
    indegree = {d: len(index[d].parent_digests) for d in index}
    children: dict[str, list[str]] = {d: [] for d in index}
    for digest, node in index.items():
        for parent in node.parent_digests:
            children[parent].append(digest)

    # Sorted frontier + sorted insertion is what makes the output order stable.
    frontier = sorted(d for d, deg in indegree.items() if deg == 0)
    order: list[ScienceNode] = []
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
        emitted = {n.node_digest for n in order}
        raise CyclicLineageError(_find_cycle(index, sorted(set(index) - emitted)))
    return order


def verify_dag(nodes: Sequence[ScienceNode]) -> DagVerification:
    """Check the lineage without raising, returning the ADR's verify flags.

    The non-raising counterpart to :func:`build_dag`, for the record-and-report
    path: a verifier walking someone else's capsule wants to *report* a broken
    lineage, not abort on it (I-3/I-4). ``sealed_into_root`` is left ``None``
    because this function checks the DAG and nothing else — see
    :class:`DagVerification` on why an unperformed check must not become a flag.
    """
    try:
        build_dag(nodes)
    except UnresolvedParentError:
        return DagVerification(dag_walk_ok=False, acyclic=None, no_broken_parent=False)
    except CyclicLineageError:
        # `no_broken_parent` is True here on purpose: every parent resolved —
        # that is precisely how the walk got far enough to find a cycle.
        return DagVerification(dag_walk_ok=False, acyclic=False, no_broken_parent=True)
    except ScienceProvenanceError:
        # Duplicate digests / oversize facet: the walk did not complete, but
        # neither acyclicity nor parent resolution was established either way,
        # so both stay `None` rather than being guessed.
        return DagVerification(dag_walk_ok=False)
    return DagVerification(dag_walk_ok=True, acyclic=True, no_broken_parent=True)


def build_facet(
    nodes: Iterable[ScienceNode],
    *,
    bound_root: str | None = None,
    verified: DagVerification | None = None,
) -> ScienceProvenanceFacet:
    """Assemble the science-provenance facet from a declared lineage.

    Nodes are stored in :func:`build_dag` order. An empty lineage yields an
    empty facet rather than an error — absent science material is the normal
    case and must not raise on the capture path (I-3); :func:`attach_facet` is
    what turns "no material" into "no facet".

    Raises:
        ScienceProvenanceError: if the declared lineage is malformed. A cycle or
            a dangling parent is *caller* input, not runtime absence, and I-3's
            fail-open rule covers missing material — not incoherent material.
            Writing a known-broken DAG into a sealed capsule would be worse than
            writing none.
    """
    ordered = build_dag(list(nodes))
    return ScienceProvenanceFacet(
        hypothesis_experiment_result=ordered,
        bound_root=bound_root,
        verified=verified,
    )


def attach_facet(
    capsule: dict[str, Any], facet: ScienceProvenanceFacet
) -> dict[str, Any]:
    """Attach the science-provenance facet to a capsule dict, additively.

    Writes nothing when the facet carries no lineage: a run that is not science
    must be byte-identical to one captured before this feature existed (I-1,
    I-3). Returns a new dict; the input is not mutated.
    """
    if not facet.has_material:
        return capsule
    out = dict(capsule)
    facets = dict(out.get("facets") or {})
    # exclude_none so an unchecked verification flag is *absent*, not `null` —
    # see DagVerification: absent means "not checked" and that distinction has
    # to survive serialisation to be worth anything.
    facets[FACET_NAME] = facet.model_dump(exclude_none=True)
    out["facets"] = facets
    return out


def facet_from_capsule(capsule: dict[str, Any]) -> ScienceProvenanceFacet | None:
    """Read the science-provenance facet back out of a capsule dict.

    Returns None when the capsule has no facet — the overwhelmingly common case,
    and not an error (I-3).
    """
    facets = capsule.get("facets")
    if not isinstance(facets, dict):
        return None
    block = facets.get(FACET_NAME)
    if not isinstance(block, dict):
        return None
    return ScienceProvenanceFacet.model_validate(block)


def verify_node_digest(node: ScienceNode, content: str | bytes) -> bool:
    """Re-verify a node's digest against the content it claims to bind.

    The only thing in this module that checks a *claim* rather than a structure,
    and it checks identity only: that these bytes are what the node names. It
    says nothing about whether the hypothesis was sound or the result true (I-4).
    """
    return node.node_digest == digest_node(content)
