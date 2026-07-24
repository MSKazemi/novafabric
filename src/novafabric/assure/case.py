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

"""Machine-checkable assurance-case argument graph — ADR-0166 D1/P1 (NF-341/342).

An assurance case is a GSN/SACM/CAE node graph — ``goal / strategy / solution /
context / assumption / justification`` — whose ``solution`` nodes bind to sealed
capsule roots **by digest only** (never clause bodies, findings, or PII). This
module models that graph, validates its structural invariants, and attaches it
to a capsule as the optional ``facets.assurance_case`` block.

Four invariants from ADR-0166 shape every choice here:

- **I-1 Additive-first.** The facet lives in optional ``facets.assurance_case``.
  A capsule carrying no assurance case stays exactly as valid as it was before
  this feature existed, byte for byte.
- **I-2 No payloads.** Evidence is bound by reference and digest. Clause bodies,
  assessor findings text, and PII never enter the capsule through this path
  (ADR-0021 §4, ADR-0009).
- **I-3 Absent is not false.** A ``solution`` node whose binding does not
  resolve is an ``unsupported_leaf`` — *unresolved*, recorded, non-fatal. It is
  never reported as satisfied and never as refuted.
- **I-4 Records the argument, never rules on it.** NovaFabric checks *structure*
  — single top goal, acyclic, no orphan, references resolve. It never asserts
  the argument is sound, the evidence sufficient, or the system acceptable.
  That judgement stays with the human assessor.

The structural checks:

* exactly one **top goal** (a ``goal`` node no other node supports),
* the ``supported_by`` graph is **acyclic**,
* **no orphan** — every node is reachable from the top goal,
* node ids are unique and every ``supported_by`` reference resolves.

P1 is the graph, the leaf binding, and the facet only. The currency ledger (D2),
conformance map (D3), defeaters and coverage (D4), and the assessor package (D5)
live in sibling modules; the facet's ``extra="allow"`` config is what lets a
later slice extend it without a schema break.

Two entry points, deliberately:

* :func:`build_case` **raises** a named error at the first structural defect. It
  is the build path — writing a known-broken argument into a sealed capsule is
  worse than writing none.
* :func:`validate_case` **reports** every defect as a string, without raising.
  It is the verifier path: something walking a capsule it did not produce wants
  to report a broken argument, not abort on it (I-3/I-4).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

FACET_NAME = "assurance_case"
SCHEMA_VERSION = "0.1.0"

#: Upper bound on nodes in one case. Every traversal below is bounded by this,
#: so a hostile or corrupt argument cannot make an offline verifier — which
#: walks capsules it did not produce — loop or allocate without limit.
MAX_CASE_NODES = 100_000


class NodeType(str, Enum):
    goal = "goal"
    strategy = "strategy"
    solution = "solution"
    context = "context"
    assumption = "assumption"
    justification = "justification"


# ── Errors ────────────────────────────────────────────────────────────────


class AssuranceCaseError(Exception):
    """Base class for every error this module raises.

    Subclasses :class:`Exception`, not :class:`ValueError`: a malformed argument
    is a structural evidence problem a caller should handle by name, and
    catching ``ValueError`` around a case build would also swallow unrelated
    coercion failures from anywhere in the call stack. (Pydantic v2 additionally
    folds a validator's ``ValueError`` into ``ValidationError``, which would
    destroy the named type at exactly the boundary it is most useful.)
    """


class CaseTooLargeError(AssuranceCaseError):
    """Raised when a case declares more than :data:`MAX_CASE_NODES` nodes."""


class DuplicateNodeIdError(AssuranceCaseError):
    """Raised when two nodes share an ``id``.

    ``supported_by`` resolves *by id*, so two nodes carrying the same id make
    every edge pointing at them ambiguous. Rather than pick one, this is
    rejected: an ambiguous argument silently resolved is worse evidence than no
    argument at all.
    """


class UnresolvedNodeRefError(AssuranceCaseError):
    """Raised when a ``supported_by`` entry names no node in the case.

    Carries ``node_id`` and ``ref`` so the caller can name the break. The
    alternative — dropping the dangling edge — would render an *incomplete*
    argument indistinguishable from a complete one, which is the single most
    misleading thing this module could do.
    """

    def __init__(self, node_id: str, ref: str) -> None:
        self.node_id = node_id
        self.ref = ref
        super().__init__(
            f"node {node_id!r} references unknown node {ref!r}; an unresolved "
            "reference is recorded or raised, never dropped"
        )


class NoTopGoalError(AssuranceCaseError):
    """Raised when no ``goal`` node is left unsupported.

    Distinct from :class:`MultipleTopGoalsError` on purpose: no top goal means
    the argument has no conclusion — usually a cycle through the goals — while
    two top goals means it has two, which is two arguments in one document. The
    two want different fixes from whoever reads the traceback.
    """


class MultipleTopGoalsError(AssuranceCaseError):
    """Raised when more than one ``goal`` node is unsupported.

    Carries ``top_goals`` (sorted) because "multiple top goals" is not
    actionable and "['G1', 'G2']" is.
    """

    def __init__(self, top_goals: Sequence[str]) -> None:
        self.top_goals = sorted(top_goals)
        super().__init__(
            f"multiple top goals: {self.top_goals}; an assurance case argues "
            "toward exactly one top-level claim"
        )


class OrphanNodeError(AssuranceCaseError):
    """Raised when a node is unreachable from the top goal.

    Carries ``orphans`` (sorted). An orphan is surfaced rather than dropped
    because an argument with unreachable nodes is *incomplete*, and silently
    omitting them would make an incomplete argument look complete — the exact
    failure mode ADR-0166 exists to prevent.
    """

    def __init__(self, orphans: Sequence[str]) -> None:
        self.orphans = sorted(orphans)
        super().__init__(
            f"orphan node(s) not reachable from top goal: {self.orphans}"
        )


class CyclicArgumentError(AssuranceCaseError):
    """Raised when the ``supported_by`` graph contains a cycle.

    Carries ``cycle`` — the node ids on the cycle, in walk order, with the entry
    node repeated at the end — because "this graph has a cycle" is not
    actionable and "G1 -> S1 -> G1" is. A claim supported by evidence that
    depends on the claim is not an argument; it is circular reasoning made
    machine-readable, and ADR-0166 D1 requires an acyclic graph.
    """

    def __init__(self, cycle: Sequence[str]) -> None:
        self.cycle = list(cycle)
        super().__init__("cycle detected in the supported_by graph: " + " -> ".join(self.cycle))


# ── Objects ───────────────────────────────────────────────────────────────


class EvidenceRef(BaseModel):
    """A reference from a solution node to a sealed capsule root — digest only.

    Two fields and no third: there is deliberately nowhere here to put a clause
    body, a findings string, or an assessor's name (I-2). ``tests`` assert this
    field set exactly, so widening it is a reviewed decision rather than a drift.
    """

    ref: str  # capsule root / bundle identifier (a reference, never a body)
    digest: str  # content digest that binds the reference (e.g. sha256 hex)


class AssuranceNode(BaseModel):
    id: str
    type: NodeType
    statement: str  # the argument text — never findings or PII
    supported_by: list[str] = []  # child node ids
    evidence_refs: list[EvidenceRef] = []  # meaningful for `solution` nodes


class AssuranceCase(BaseModel):
    case_id: str
    nodes: list[AssuranceNode] = []


class CaseValidation(BaseModel):
    """Result of validating an assurance case's structure and evidence resolution."""

    valid: bool  # True when there are no fatal structural errors
    errors: list[str] = []  # fatal: dup id, unknown ref, top-goal count, cycle, orphan
    top_goal_id: str | None = None
    unsupported_leaves: list[str] = []  # solution nodes with no resolvable evidence


class CaseVerification(BaseModel):
    """What a verifier actually checked about the argument (I-4).

    Every flag defaults to ``None``, meaning *not checked* — distinct from
    ``False``, meaning *checked and failed*. P1 performs no signature or seal
    verification, so ``sealed_into_root`` stays ``None`` unless a caller that did
    the check sets it; defaulting it to ``True`` would launder an unperformed
    check into the sealed record, and defaulting it to ``False`` would slander a
    seal nobody looked at. Absent is not false.

    Note what is *not* here: any field meaning "the argument is sound" or "the
    case is sufficient". Those are the assessor's to decide and ADR-0166's
    sharpest boundary risk — a structurally complete, current argument can still
    be a bad argument (I-4, alternative 2).
    """

    model_config = ConfigDict(extra="allow")

    graph_walk_ok: bool | None = None
    acyclic: bool | None = None
    single_top_goal: bool | None = None
    no_orphan: bool | None = None
    sealed_into_root: bool | None = None


class AssuranceCaseFacet(BaseModel):
    """The optional ``facets.assurance_case`` block (NF-341, I-1)."""

    model_config = ConfigDict(extra="allow")

    schema_version: str = SCHEMA_VERSION
    case_id: str | None = None
    nodes: list[AssuranceNode] = Field(default_factory=list)
    top_goal_id: str | None = None
    #: Solution nodes whose binding did not resolve at build time. Recorded in
    #: the facet rather than left to be recomputed, because the set of digests
    #: that resolved is a property of *when and where* the case was assembled,
    #: and a later reader will not have that context (I-3).
    unsupported_leaves: list[str] = Field(default_factory=list)
    #: Digest of the sealed capsule root this facet is bound into. Optional in
    #: P1: the root is only known once the capsule is sealed, and a facet built
    #: during a run legitimately does not have it yet.
    bound_root: str | None = None
    verified: CaseVerification | None = None

    @property
    def has_material(self) -> bool:
        """True when the facet carries an argument the capsule did not already have."""
        return bool(self.nodes)


# ── Digest handling ───────────────────────────────────────────────────────


def _normalise_digest(digest: str) -> str:
    """Return ``digest`` without a ``sha256:`` prefix, for comparison only.

    The rest of the capsule writes digests as ``sha256:<hex>`` (see
    ``science/provenance.py``), but this module shipped accepting bare hex and
    its callers — ``assure/package.py`` and the D3/D4 modules — pass bare hex
    today. Normalising at the comparison point accepts both forms, so a facet
    built against capsule-style digests resolves against a bare-hex resolvable
    set and vice versa.

    Deliberately *permissive rather than strict*: tightening ``EvidenceRef`` to
    require one form would reject arguments that already validate, which I-1
    forbids. The cost is that this module cannot tell a malformed digest from an
    unrecognised one — both simply fail to resolve and surface as an
    ``unsupported_leaf``, which is the honest outcome either way (I-3).
    """
    return digest[7:] if digest.startswith("sha256:") else digest


# ── Graph traversal ───────────────────────────────────────────────────────


def _find_cycle(edges: dict[str, list[str]], remaining: Iterable[str]) -> list[str]:
    """Return one concrete cycle path from the nodes Kahn's algorithm rejected.

    Walks ``supported_by`` edges iteratively — never recursively, and bounded by
    the node count — because a corrupt case is exactly the input that would blow
    a recursive walk's stack, and this code runs inside offline verifiers on
    capsules they did not produce (the repo's bounded-recursion rule).
    """
    remaining_set = set(remaining)
    start = min(remaining_set)  # min, not next(iter(...)): deterministic reporting
    path: list[str] = []
    seen: dict[str, int] = {}
    current = start
    for _ in range(len(edges) + 1):
        if current in seen:
            # Trim the lead-in: report the cycle itself, not the walk that
            # happened to reach it.
            return [*path[seen[current] :], current]
        seen[current] = len(path)
        path.append(current)
        # Only children still in `remaining` lead back into a cycle; picking the
        # lexicographically smallest keeps the reported path deterministic.
        children = sorted(c for c in edges.get(current, []) if c in remaining_set)
        if not children:
            break
        current = children[0]
    # Unreachable for a graph Kahn's algorithm left behind (every such node has
    # at least one unresolved parent, so the walk cannot terminate before
    # revisiting), but returning the path is better than raising here.
    return path


def _detect_cycle(node_ids: list[str], edges: dict[str, list[str]]) -> list[str]:
    """Return a cycle path, or ``[]`` when the ``supported_by`` graph is acyclic.

    Kahn's algorithm — iterative by construction, so an adversarial depth cannot
    exhaust the stack. Replaces the recursive depth-first colouring this module
    shipped with, which a deep chain from an untrusted capsule could overflow.
    """
    known = set(node_ids)
    indegree = dict.fromkeys(node_ids, 0)
    for parent in node_ids:
        for child in edges.get(parent, []):
            if child in known:
                indegree[child] += 1

    frontier = [n for n, deg in indegree.items() if deg == 0]
    emitted = 0
    while frontier:
        node = frontier.pop()
        emitted += 1
        for child in edges.get(node, []):
            if child in known:
                indegree[child] -= 1
                if indegree[child] == 0:
                    frontier.append(child)

    if emitted == len(node_ids):
        return []
    return _find_cycle(edges, [n for n, deg in indegree.items() if deg > 0])


def _reachable_from(start: str, edges: dict[str, list[str]]) -> set[str]:
    """Return every node reachable from ``start``, walking iteratively."""
    seen: set[str] = set()
    stack = [start]
    while stack:
        n = stack.pop()
        if n in seen:
            continue
        seen.add(n)
        stack.extend(edges.get(n, []))
    return seen


def _top_goals(case: AssuranceCase, edges: dict[str, list[str]]) -> list[str]:
    """Return the ``goal`` nodes no other node supports."""
    supported_ids = {c for children in edges.values() for c in children}
    return [
        n.id for n in case.nodes if n.type == NodeType.goal and n.id not in supported_ids
    ]


def _unsupported_leaves(
    case: AssuranceCase, resolvable_digests: Iterable[str]
) -> list[str]:
    """Return the ``solution`` nodes with no resolvable evidence binding (I-3).

    A solution whose binding does not resolve is *unresolved*, not refuted and
    not satisfied. Only ``solution`` nodes are considered: a ``context`` or
    ``assumption`` leaf carries no evidence by design, and flagging it would
    manufacture a gap that the argument's notation never claimed to fill.
    """
    resolvable = {_normalise_digest(d) for d in resolvable_digests}
    return sorted(
        n.id
        for n in case.nodes
        if n.type == NodeType.solution
        and not any(_normalise_digest(ref.digest) in resolvable for ref in n.evidence_refs)
    )


# ── Case construction ─────────────────────────────────────────────────────


def build_case(case: AssuranceCase) -> list[AssuranceNode]:
    """Validate the argument graph and return its nodes, or raise naming the defect.

    Returns the nodes in a **deterministic order**: breadth-first from the top
    goal, with each node's children visited in sorted order.

    That order is a *serialisation* guarantee, not a claim about argument
    strength or evaluation sequence. It exists so that two runs declaring the
    same argument produce the same facet bytes — the facet is hashed into the
    seal, and a stable ordering is what keeps that hash comparable.

    Checks run in dependency order, so the error a caller sees names the cause
    rather than a downstream symptom: a cycle through the goals also produces
    zero top goals, and "cycle: G1 -> G2 -> G1" is the actionable half of that.

    Raises:
        CaseTooLargeError: more than :data:`MAX_CASE_NODES` nodes.
        DuplicateNodeIdError: two nodes share an ``id``.
        UnresolvedNodeRefError: a ``supported_by`` entry names no node.
        CyclicArgumentError: the graph contains a cycle. A node that supports
            itself is a cycle of length one and is rejected here.
        NoTopGoalError / MultipleTopGoalsError: not exactly one top goal.
        OrphanNodeError: a node is unreachable from the top goal.
    """
    if len(case.nodes) > MAX_CASE_NODES:
        raise CaseTooLargeError(
            f"{len(case.nodes)} nodes exceeds the {MAX_CASE_NODES}-node limit; an "
            "offline verifier must be able to walk this case in bounded work"
        )

    by_id: dict[str, AssuranceNode] = {}
    for node in case.nodes:
        if node.id in by_id:
            raise DuplicateNodeIdError(
                f"duplicate node id: {node.id!r}; supported_by resolves by id, "
                "so this argument is ambiguous"
            )
        by_id[node.id] = node

    edges = {n.id: list(n.supported_by) for n in case.nodes}

    for node in case.nodes:
        for ref in node.supported_by:
            if ref not in by_id:
                raise UnresolvedNodeRefError(node.id, ref)

    cycle = _detect_cycle(list(by_id), edges)
    if cycle:
        raise CyclicArgumentError(cycle)

    tops = _top_goals(case, edges)
    if len(tops) == 0:
        raise NoTopGoalError(
            "no top goal: exactly one unsupported `goal` node is required"
        )
    if len(tops) > 1:
        raise MultipleTopGoalsError(tops)

    reachable = _reachable_from(tops[0], edges)
    orphans = [nid for nid in by_id if nid not in reachable]
    if orphans:
        raise OrphanNodeError(orphans)

    # Deterministic breadth-first order from the top goal. The graph is acyclic
    # and orphan-free by this point, so this reaches every node exactly once.
    order: list[AssuranceNode] = []
    emitted: set[str] = set()
    frontier = [tops[0]]
    while frontier:
        nid = frontier.pop(0)
        if nid in emitted:
            continue  # a diamond: two parents converge on one child
        emitted.add(nid)
        order.append(by_id[nid])
        frontier.extend(sorted(edges[nid]))
    return order


def validate_case(
    case: AssuranceCase,
    resolvable_digests: frozenset[str] | set[str] = frozenset(),
) -> CaseValidation:
    """Validate an assurance case's structure and resolve its evidence leaves.

    The non-raising counterpart to :func:`build_case`, for the record-and-report
    path: a verifier walking someone else's capsule wants to *report* a broken
    argument, not abort on it (I-3/I-4).

    Unlike :func:`build_case`, this accumulates *every* defect rather than
    stopping at the first — a reviewer fixing an argument wants the whole list,
    and a graph with a cycle through its goals genuinely has both a cycle and no
    top goal.

    ``resolvable_digests`` is the set of capsule-root digests known to resolve
    offline; a ``solution`` node counts as supported only if at least one of its
    evidence refs is in that set. Digests compare with or without the
    ``sha256:`` prefix — see :func:`_normalise_digest`.
    """
    errors: list[str] = []

    # Size first: every traversal below is bounded by the node count, so an
    # oversize case is rejected before anything walks it. Reported rather than
    # raised, to keep this function's no-raise contract.
    if len(case.nodes) > MAX_CASE_NODES:
        return CaseValidation(
            valid=False,
            errors=[
                f"{len(case.nodes)} nodes exceeds the {MAX_CASE_NODES}-node limit"
            ],
        )

    ids = [n.id for n in case.nodes]

    # Duplicate ids — checked first; downstream logic assumes unique ids.
    seen: set[str] = set()
    dups: set[str] = set()
    for nid in ids:
        (dups if nid in seen else seen).add(nid)
    for d in sorted(dups):
        errors.append(f"duplicate node id: {d!r}")

    by_id = {n.id: n for n in case.nodes}
    edges = {n.id: list(n.supported_by) for n in case.nodes}

    # Unknown supported_by references.
    for n in case.nodes:
        for child in n.supported_by:
            if child not in by_id:
                errors.append(f"node {n.id!r} references unknown node {child!r}")

    # Top goal: a `goal` node that no node supports.
    tops = _top_goals(case, edges)
    top_goal_id: str | None = None
    if len(tops) == 1:
        top_goal_id = tops[0]
    elif len(tops) == 0:
        errors.append("no top goal: exactly one unsupported `goal` node is required")
    else:
        errors.append(f"multiple top goals: {sorted(tops)}")

    # Cycle detection. The reported path is what makes this actionable.
    cycle = _detect_cycle(list(by_id), edges)
    if cycle:
        errors.append("cycle detected in the supported_by graph: " + " -> ".join(cycle))

    # Orphans: nodes unreachable from the top goal (only when the graph is
    # acyclic and has a single top goal — otherwise the earlier error is the
    # real cause and an orphan list would be noise).
    if top_goal_id is not None and not cycle:
        reachable = _reachable_from(top_goal_id, edges)
        orphans = sorted(nid for nid in by_id if nid not in reachable)
        if orphans:
            errors.append(f"orphan node(s) not reachable from top goal: {orphans}")

    return CaseValidation(
        valid=not errors,
        errors=errors,
        top_goal_id=top_goal_id,
        unsupported_leaves=_unsupported_leaves(case, resolvable_digests),
    )


def verify_case(
    case: AssuranceCase,
) -> CaseVerification:
    """Check the argument without raising, returning the ADR's verify flags.

    Flags stay ``None`` for anything this call did not establish — see
    :class:`CaseVerification` on why an unperformed check must not become a flag.
    """
    try:
        build_case(case)
    except (CaseTooLargeError, DuplicateNodeIdError):
        # The walk never started, so neither acyclicity nor the goal structure
        # was established either way; both stay `None` rather than being guessed.
        return CaseVerification(graph_walk_ok=False)
    except UnresolvedNodeRefError:
        return CaseVerification(graph_walk_ok=False)
    except CyclicArgumentError:
        return CaseVerification(graph_walk_ok=False, acyclic=False)
    except (NoTopGoalError, MultipleTopGoalsError):
        # `acyclic` is True here on purpose: the cycle check ran and passed —
        # that is precisely how the walk got as far as counting top goals.
        return CaseVerification(graph_walk_ok=False, acyclic=True, single_top_goal=False)
    except OrphanNodeError:
        return CaseVerification(
            graph_walk_ok=False, acyclic=True, single_top_goal=True, no_orphan=False
        )
    return CaseVerification(
        graph_walk_ok=True, acyclic=True, single_top_goal=True, no_orphan=True
    )


# ── Facet ─────────────────────────────────────────────────────────────────


def build_facet(
    case: AssuranceCase,
    *,
    resolvable_digests: frozenset[str] | set[str] = frozenset(),
    bound_root: str | None = None,
) -> AssuranceCaseFacet:
    """Assemble the assurance-case facet from a validated argument graph.

    Nodes are stored in :func:`build_case` order. An empty case yields an empty
    facet rather than an error — absent assurance material is the normal case
    and must not raise on the capture path; :func:`attach_facet` is what turns
    "no material" into "no facet".

    Raises:
        AssuranceCaseError: if the argument graph is malformed. A cycle, a
            dangling reference, or a missing top goal is *caller* input, not
            runtime absence: the fail-open rule covers missing material, not
            incoherent material. Writing a known-broken argument into a sealed
            capsule would be worse than writing none.
    """
    if not case.nodes:
        return AssuranceCaseFacet(case_id=case.case_id, bound_root=bound_root)

    ordered = build_case(case)
    edges = {n.id: list(n.supported_by) for n in case.nodes}
    return AssuranceCaseFacet(
        case_id=case.case_id,
        nodes=ordered,
        top_goal_id=_top_goals(case, edges)[0],
        unsupported_leaves=_unsupported_leaves(case, resolvable_digests),
        bound_root=bound_root,
        verified=CaseVerification(
            graph_walk_ok=True, acyclic=True, single_top_goal=True, no_orphan=True
        ),
    )


def attach_facet(
    capsule: dict[str, Any], facet: AssuranceCaseFacet
) -> dict[str, Any]:
    """Attach the assurance-case facet to a capsule dict, additively.

    Writes nothing when the facet carries no argument: a run with no assurance
    material must be byte-identical to one captured before this feature existed
    (I-1). Returns a new dict; the input is not mutated.
    """
    if not facet.has_material:
        return capsule
    out = dict(capsule)
    facets = dict(out.get("facets") or {})
    # exclude_none so an unchecked verification flag is *absent*, not `null` —
    # see CaseVerification: absent means "not checked" and that distinction has
    # to survive serialisation to be worth anything.
    facets[FACET_NAME] = facet.model_dump(exclude_none=True, mode="json")
    out["facets"] = facets
    return out


def facet_from_capsule(capsule: dict[str, Any]) -> AssuranceCaseFacet | None:
    """Read the assurance-case facet back out of a capsule dict.

    Returns None when the capsule has no facet — the overwhelmingly common case,
    and not an error (I-1).
    """
    facets = capsule.get("facets")
    if not isinstance(facets, dict):
        return None
    block = facets.get(FACET_NAME)
    if not isinstance(block, dict):
        return None
    return AssuranceCaseFacet.model_validate(block)
