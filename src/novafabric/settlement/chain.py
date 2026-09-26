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

"""A2A payment-provenance chain — ADR-0163 P3 (NF-315, experimental).

``facets.settlement.a2a_payment_chain`` is an ordered list of settlement hops
between agents (``payer_agent_ref → payee_agent_ref``), each binding its
settlement confirmation by digest and naming the hop it derives from in
``parent_hop``. NovaFabric **walks** the chain offline; it never moves value
between hops, and a passing walk is a statement about the *record*, not a
guarantee that any hop's money arrived (ADR-0163 I-1, I-4).

**Shape of a valid chain.** Spec §3 req. 9 fixes ``parent_hop`` as ``null`` for
the first hop and as a reference to an earlier hop otherwise; it does not
allow forks. Together with ``hop_index`` equal to the recorded position that
makes a valid chain a single path — hop *i* is funded by hop *i-1*. The
verifier still reports each fault by name rather than checking
``parent_hop == hop_index - 1`` in one line, because "which link is wrong, and
how" is the evidence a reviewer needs.

**Why a parent must be *earlier*.** Resolving ``parent_hop`` only against hops
already walked makes every accepted link point strictly backwards, so the
accepted graph cannot contain a cycle and the walk is one linear pass. Any
cycle in a recorded chain needs at least one link pointing forward (or at
itself), and that link is reported as ``forward_parent``.

**What the walk also checks.** A hop's payer must be its parent's payee
(``payer_not_prior_payee`` — otherwise the chain splices two unrelated
payments together) and its currency must equal its parent's
(``currency_mismatch`` — an FX conversion between hops cannot be verified
without a rate NovaFabric would have to choose, which is adjudication, I-4).
Both are recorded findings; neither is "corrected".
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from novafabric.settlement._refs import validate_digest, validate_identity_ref, with_block
from novafabric.settlement.facet import Money, SettlementFacet, reject_payment_secrets

#: Key of the chain inside ``facets.settlement``.
CHAIN_FIELD = "a2a_payment_chain"

#: Upper bound on hops. Far beyond a real agent-to-agent payment path, and it
#: keeps a hostile capsule from turning an offline walk into unbounded work.
MAX_CHAIN_HOPS = 1024

A2AChainFindingCode = Literal[
    "empty_chain",
    "chain_too_long",
    "hop_index_mismatch",
    "duplicate_hop_index",
    "first_hop_has_parent",
    "missing_parent",
    "dangling_parent",
    "forward_parent",
    "fork",
    "payer_not_prior_payee",
    "currency_mismatch",
]

#: Which verdict each finding code falsifies. Every code is mapped, so no
#: finding can exist that leaves every verdict true.
_ORDER_CODES = frozenset(
    {"empty_chain", "chain_too_long", "hop_index_mismatch", "duplicate_hop_index"}
)
_PARENT_CODES = frozenset({"first_hop_has_parent", "missing_parent", "dangling_parent"})
_CYCLE_CODES = frozenset({"forward_parent"})
_FORK_CODES = frozenset({"fork"})
_CONTINUITY_CODES = frozenset({"payer_not_prior_payee"})
_CURRENCY_CODES = frozenset({"currency_mismatch"})


class MalformedA2AChainError(Exception):
    """Raised when a stored chain cannot be read as a list of hops.

    A malformed hop is not something a walk can reason about, and an
    over-long chain is refused *before* its hops are parsed, so reading is
    bounded too.
    """


class BrokenA2AChainError(Exception):
    """Raised when a producer asks to record a chain whose walk fails.

    Carries the full :class:`A2AChainVerification` so the caller sees every
    fault, not only the first. Recording a broken chain as if it were intact
    would hand a later dispute a provenance path that never existed.
    """

    def __init__(self, verification: A2AChainVerification) -> None:
        self.verification = verification
        codes = sorted({finding.code for finding in verification.findings})
        super().__init__(
            f"A2A payment chain fails the offline walk ({', '.join(codes)}); "
            "a broken chain is never recorded as intact (ADR-0163 D3, NF-315)"
        )


class A2APaymentHop(BaseModel):
    """One agent-to-agent settlement hop (NF-315).

    ``parent_hop`` is required-but-nullable: the first hop says ``null``
    explicitly, so "this is the origin" and "the parent was lost" can never be
    confused. Integers are strict — a float or bool ``hop_index`` is refused
    rather than coerced.
    """

    model_config = ConfigDict(extra="allow")

    hop_index: int = Field(ge=0, lt=MAX_CHAIN_HOPS, strict=True)
    payer_agent_ref: str
    payee_agent_ref: str
    #: Integer minor units + ISO-4217 code (see :class:`Money`); never a float.
    amount: Money
    #: Digest of this hop's settlement confirmation.
    settlement_ref: str
    parent_hop: int | None = Field(ge=0, lt=MAX_CHAIN_HOPS, strict=True)

    @field_validator("payer_agent_ref", "payee_agent_ref", mode="before")
    @classmethod
    def _check_identity(cls, value: object, info: Any) -> str:
        return validate_identity_ref(value, field=info.field_name)

    @field_validator("settlement_ref", mode="before")
    @classmethod
    def _check_settlement_ref(cls, value: object) -> str:
        return validate_digest(value, field="settlement_ref")

    @model_validator(mode="after")
    def _reject_secrets(self) -> A2APaymentHop:
        """ADR-0163 I-2 on the hop itself, ``extra`` fields included."""
        reject_payment_secrets(self.model_dump(), path=f"{CHAIN_FIELD}[{self.hop_index}]")
        return self


class A2AChainFinding(BaseModel):
    """One fault found while walking the chain."""

    model_config = ConfigDict(frozen=True)

    #: Recorded position of the hop the fault was found on.
    position: int
    code: A2AChainFindingCode
    message: str


class A2AChainVerification(BaseModel):
    """Outcome of an offline walk (spec §6: ``acyclic`` + ``no_broken_parent``).

    Every boolean is derived from ``findings``, so verdicts and reasons can
    never disagree. ``moved_value`` is a constant ``False``: it is on the
    record so no reader can take a passing walk for a value transfer.
    """

    model_config = ConfigDict(frozen=True)

    hop_count: int
    ordered: bool
    no_broken_parent: bool
    acyclic: bool
    linear: bool
    continuous: bool
    currency_consistent: bool
    moved_value: Literal[False] = False
    findings: list[A2AChainFinding] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True only when every property holds (fail-closed)."""
        return (
            self.ordered
            and self.no_broken_parent
            and self.acyclic
            and self.linear
            and self.continuous
            and self.currency_consistent
        )


# ── Reading ───────────────────────────────────────────────────────────────


def chain_from_facet(facet: SettlementFacet) -> list[A2APaymentHop] | None:
    """Return the facet's A2A chain in recorded order, or ``None`` if absent.

    The order is never sorted: the recorded order *is* the evidence, and
    sorting would repair exactly the faults the walk must report.

    Raises:
        MalformedA2AChainError: if the stored value is not a list, is longer
            than :data:`MAX_CHAIN_HOPS` (checked before any hop is parsed), or
            holds a hop that fails validation.
        PaymentSecretRejectedError: if a hop carries a payment secret.
        InvalidReferenceError: if a hop's ``settlement_ref`` is not a digest.
    """
    raw = (facet.model_extra or {}).get(CHAIN_FIELD)
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise MalformedA2AChainError(
            f"{CHAIN_FIELD} must be a list of hops, got {type(raw).__name__}"
        )
    if len(raw) > MAX_CHAIN_HOPS:
        raise MalformedA2AChainError(
            f"{CHAIN_FIELD} has {len(raw)} hops; refusing to read more than {MAX_CHAIN_HOPS}"
        )
    return [_parse_hop(item, index) for index, item in enumerate(raw)]


def _parse_hop(item: object, index: int) -> A2APaymentHop:
    """Validate one stored hop, naming its position on failure."""
    if isinstance(item, A2APaymentHop):
        return item
    try:
        return A2APaymentHop.model_validate(item)
    except ValidationError as exc:
        first = exc.errors(include_url=False)[0]
        where = ".".join(str(part) for part in first["loc"]) or "hop"
        raise MalformedA2AChainError(f"{CHAIN_FIELD}[{index}].{where}: {first['msg']}") from exc


# ── Walking ───────────────────────────────────────────────────────────────


def _finding(position: int, code: A2AChainFindingCode, message: str) -> A2AChainFinding:
    return A2AChainFinding(position=position, code=code, message=message)


def _link_findings(
    position: int,
    hop: A2APaymentHop,
    seen: Mapping[int, int],
    hops: Sequence[A2APaymentHop],
    used_parents: set[int],
) -> list[A2AChainFinding]:
    """Findings for one hop's ``parent_hop`` link (resolved among earlier hops)."""
    parent = hop.parent_hop
    if position == 0:
        if parent is None:
            return []
        return [
            _finding(
                0,
                "first_hop_has_parent",
                f"the first hop must have parent_hop null, got {parent}",
            )
        ]
    if parent is None:
        return [
            _finding(
                position,
                "missing_parent",
                "hop has parent_hop null but is not the first hop; the chain "
                "splits into two unrelated origins",
            )
        ]
    if parent >= hop.hop_index:
        return [
            _finding(
                position,
                "forward_parent",
                f"parent_hop {parent} does not precede hop_index {hop.hop_index}; "
                "a link to itself or a later hop can close a cycle",
            )
        ]
    parent_position = seen.get(parent)
    if parent_position is None:
        return [
            _finding(
                position,
                "dangling_parent",
                f"parent_hop {parent} resolves to no earlier hop in this chain",
            )
        ]
    findings: list[A2AChainFinding] = []
    if parent in used_parents:
        findings.append(
            _finding(
                position,
                "fork",
                f"hop {parent} is already the parent of another hop; the chain "
                "is ordered and does not fork (spec §3 req. 9)",
            )
        )
    used_parents.add(parent)
    parent_hop = hops[parent_position]
    if parent_hop.payee_agent_ref != hop.payer_agent_ref:
        findings.append(
            _finding(
                position,
                "payer_not_prior_payee",
                f"payer_agent_ref is not the payee of parent hop {parent}; the "
                "chain splices two unrelated payments",
            )
        )
    if parent_hop.amount.currency != hop.amount.currency:
        findings.append(
            _finding(
                position,
                "currency_mismatch",
                f"currency {hop.amount.currency} differs from parent hop {parent}'s "
                f"{parent_hop.amount.currency}; no FX rate is chosen (ADR-0163 I-4)",
            )
        )
    return findings


def verify_a2a_chain(hops: Sequence[A2APaymentHop]) -> A2AChainVerification:
    """Walk an A2A payment chain offline and report every fault.

    One linear pass, bounded by :data:`MAX_CHAIN_HOPS`. Never raises on a
    chain fault — a verifier that stopped at the first one would hide the
    rest. Pure: no IO, no network, no clock; it moves no value.

    An empty chain is *not* ``ok``: a recorded chain with nothing in it walks
    nothing, and "nothing was checked" must never read as "checked and intact".
    """
    if not hops:
        return _verdict(0, [_finding(0, "empty_chain", "the chain records no hops")])
    if len(hops) > MAX_CHAIN_HOPS:
        return _verdict(
            len(hops),
            [
                _finding(
                    MAX_CHAIN_HOPS,
                    "chain_too_long",
                    f"chain exceeds {MAX_CHAIN_HOPS} hops; refusing to walk it",
                )
            ],
        )
    findings: list[A2AChainFinding] = []
    #: hop_index → recorded position, for hops already walked (earlier only).
    seen: dict[int, int] = {}
    used_parents: set[int] = set()
    for position, hop in enumerate(hops):
        if hop.hop_index != position:
            findings.append(
                _finding(
                    position,
                    "hop_index_mismatch",
                    f"hop_index {hop.hop_index} recorded at position {position}",
                )
            )
        if hop.hop_index in seen:
            findings.append(
                _finding(
                    position,
                    "duplicate_hop_index",
                    f"hop_index {hop.hop_index} already used at position {seen[hop.hop_index]}",
                )
            )
        findings.extend(_link_findings(position, hop, seen, hops, used_parents))
        seen.setdefault(hop.hop_index, position)
    return _verdict(len(hops), findings)


def _verdict(hop_count: int, findings: list[A2AChainFinding]) -> A2AChainVerification:
    codes = {finding.code for finding in findings}
    too_long = "chain_too_long" in codes
    return A2AChainVerification(
        hop_count=hop_count,
        ordered=not codes & _ORDER_CODES,
        no_broken_parent=not too_long and not codes & _PARENT_CODES,
        acyclic=not too_long and not codes & _CYCLE_CODES,
        linear=not too_long and not codes & _FORK_CODES,
        continuous=not too_long and not codes & _CONTINUITY_CODES,
        currency_consistent=not too_long and not codes & _CURRENCY_CODES,
        findings=findings,
    )


def walk_back(hops: Sequence[A2APaymentHop], depth: int) -> list[A2APaymentHop]:
    """Return up to ``depth`` hops, newest first, following ``parent_hop``.

    The provenance question is "where did the money the last agent received
    come from", so the walk starts at the newest hop and follows parents
    backwards. Only links that resolve strictly backwards are followed, so the
    walk terminates on any input, in at most ``min(depth, len(hops))`` steps.
    It is a *view*; the verdict comes from :func:`verify_a2a_chain`.
    """
    if not hops or depth < 1:
        return []
    position_of: dict[int, int] = {}
    for index, item in enumerate(hops[:MAX_CHAIN_HOPS]):
        position_of.setdefault(item.hop_index, index)
    out: list[A2APaymentHop] = []
    current: int | None = min(len(hops), MAX_CHAIN_HOPS) - 1
    while current is not None and len(out) < depth:
        hop = hops[current]
        out.append(hop)
        parent = hop.parent_hop
        found = position_of.get(parent) if parent is not None else None
        current = found if found is not None and found < current else None
    return out


# ── Recording ─────────────────────────────────────────────────────────────


def build_a2a_chain(hops: Sequence[A2APaymentHop | Mapping[str, Any]]) -> list[A2APaymentHop]:
    """Validate and walk a producer's chain; return it only if the walk passes.

    Raises:
        MalformedA2AChainError: on a malformed hop or more than
            :data:`MAX_CHAIN_HOPS` hops.
        BrokenA2AChainError: when the walk finds any fault.
        PaymentSecretRejectedError: if a hop carries a payment secret.
        InvalidReferenceError: if a ``settlement_ref`` is not a digest.
    """
    if len(hops) > MAX_CHAIN_HOPS:
        raise MalformedA2AChainError(
            f"{CHAIN_FIELD} has {len(hops)} hops; refusing more than {MAX_CHAIN_HOPS}"
        )
    parsed = [_parse_hop(item, index) for index, item in enumerate(hops)]
    verification = verify_a2a_chain(parsed)
    if not verification.ok:
        raise BrokenA2AChainError(verification)
    return parsed


def attach_chain(facet: SettlementFacet, hops: Sequence[A2APaymentHop]) -> SettlementFacet:
    """Return a new facet carrying ``hops`` as ``a2a_payment_chain``.

    Re-validated through :class:`SettlementFacet`, so the facet-wide secret
    scan (I-2) covers the chain too. The input facet is not mutated.
    """
    return with_block(facet, CHAIN_FIELD, [hop.model_dump() for hop in hops])
