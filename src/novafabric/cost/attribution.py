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

"""Per-agent cost attribution — ADR-0146 D1/P1 (NF-141).

Splits the cost, energy, tokens and calls a run *already* captured across the
agents in its team graph, and asserts that the split **conserves**: the parts
sum to the whole, exactly.

Four invariants from ADR-0146 shape every choice in this module:

- **I-1 Record-only.** NovaFabric records what a run spent. Nothing here
  enforces a budget, throttles an agent, blocks a workload, or optimizes
  spend. A number that exceeds somebody's ceiling is recorded, not acted on.
- **I-2 Attribute, never re-capture.** Every figure is derived from records
  the capsule already holds (ADR-0132 token buckets, ADR-0133 pricing,
  ADR-0093 energy receipts). This module introduces no capture primitive and
  reads no prompt/response content — only counts, joules, and identifiers.
- **I-3 Additive-first / fail-open.** The facet lives in optional
  ``facets.cost_attribution``. A run with nothing to attribute produces no
  facet, and a capsule without one stays exactly as valid as before.
- **I-4 Basis is never laundered.** An `apportioned` figure — one divided by
  a stated key because no per-agent measurement existed — is marked as such
  and is never presented as `measured`.

**Why integers everywhere.** ADR-0146's illustrative JSON shows `usd: 0.412`
and a `conservation.epsilon`. This module deliberately does not follow that:
money is integer minor units plus an ISO-4217 code, and energy is integer
millijoules. Binary floating point cannot represent 0.412, so a float split
does not sum to a float total — which is exactly why the ADR's example needs
an epsilon. An epsilon does not make the check tolerant of rounding; it makes
the check *unable to see* a discrepancy smaller than the epsilon, which is
precisely the size of discrepancy a mis-attribution produces. With integer
sub-units the conservation invariant is exact and `epsilon` is structurally
unnecessary, so it is absent rather than recorded as zero.

**Why a violated invariant raises.** ``build_facet`` refuses to emit a facet
whose parts do not sum to its whole. Silently rescaling the parts to fit, or
recording ``ok: false`` and carrying on, would erase the one discrepancy the
invariant exists to reveal: the numbers disagreeing *is* the finding.

**Why an absent figure is not zero.** A per-agent field that was never
measured is ``None``, and the shortfall is reported as ``unattributed`` in the
conservation block. Writing 0 would state that an agent spent nothing, which
understates a bill and is indistinguishable from an agent that genuinely
idled.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

FACET_NAME = "cost_attribution"
SCHEMA_VERSION = "0.1.0"

#: How a figure was arrived at (ADR-0146 D1, spec §3.16).
#:
#: `measured` = read from a per-agent record (a model-call row, an ADR-0093
#: receipt). `apportioned` = the run total divided by a stated key such as
#: token share, because no per-agent measurement existed. There is no third
#: value: a figure that is neither is simply absent (see the module note on
#: "absent is not zero").
Basis = Literal["measured", "apportioned"]

#: The integer dimensions attributed alongside money, as (field, unit).
#:
#: Money is handled separately because it carries a currency; these are pure
#: counts. Energy is here as *millijoules* rather than joules: joules are
#: genuinely fractional, and a float joule cannot be conserved exactly. An
#: integer sub-unit was chosen over `Decimal` so that one apportionment
#: routine (:func:`apportion`) serves money and energy alike — a `Decimal`
#: split would need its own quantisation and remainder rule, i.e. a second
#: place for the same arithmetic to be wrong. Milli- is the finest unit any
#: shipped counter resolves (RAPL reports microjoules, but a per-agent share
#: of one microjoule is noise, not evidence).
_COUNT_DIMENSIONS: tuple[tuple[str, str], ...] = (
    ("millijoules", "mJ"),
    ("tokens_in", "tokens"),
    ("tokens_out", "tokens"),
    ("calls", "calls"),
)


# ── Errors ────────────────────────────────────────────────────────────────


class ConservationError(Exception):
    """Raised when a per-agent split does not sum to the run total.

    Deliberately **not** a ``ValueError``. Pydantic folds ``ValueError`` raised
    inside a validator into a ``ValidationError`` alongside ordinary shape
    complaints, and a caller seeing "1 validation error" is likely to retry
    with adjusted numbers — which is the one response that must not happen
    here. A conservation failure means the attribution and the measurement
    disagree; the named type has to survive to the caller.
    """

    def __init__(self, dimension: str, total: int, attributed: int, unit: str) -> None:
        self.dimension = dimension
        self.total = total
        self.attributed = attributed
        self.unit = unit
        super().__init__(
            f"cost attribution does not conserve on {dimension!r}: agents "
            f"account for {attributed} {unit} of a {total} {unit} run total "
            f"(excess {attributed - total} {unit}). The split was not "
            "rescaled to fit — a discrepancy here is the finding "
            "(ADR-0146 D1, G5)."
        )


class UnapportionableError(Exception):
    """Raised when a total cannot be divided by the key it was given.

    Not a ``ValueError``, for the same reason as :class:`ConservationError`:
    the caller has to see *which* rule refused, because the correct response
    is usually to leave the agents unattributed rather than to invent a key.
    """


class CurrencyMismatchError(Exception):
    """Raised when agent amounts are not all in the run total's currency.

    Summing 100 EUR-minor and 100 JPY into 200 of nothing would produce a
    conservation check that passes on a number that is not a sum of money.
    Cross-currency attribution needs a declared rate with provenance, which
    ADR-0146 does not define; refusing is the honest option.
    """


# ── Money ─────────────────────────────────────────────────────────────────


class Money(BaseModel):
    """An attributed amount, as integer minor units plus an ISO-4217 code.

    Never a float. Binary floating point cannot represent 0.412, so four
    agents' float shares do not sum to a float run total — the conservation
    invariant would then be checking floating-point noise rather than
    attribution, and would need an epsilon large enough to hide a real
    mis-split. Minor units keep Σ parts == whole an exact integer identity.

    The currency code is what makes minor units meaningful: 500 is EUR 5.00
    but JPY 500 (zero-decimal) and KWD 0.500 (three-decimal). Storing the
    amount alone would be storing a number, not a sum of money.

    Shaped identically to :class:`novafabric.settlement.facet.Money` but
    deliberately not imported from it: that type means "an amount a payment
    network settled" and carries ADR-0163's payment-secret rejection, whereas
    this one is a derived accounting figure that never touched a payment rail.
    Reusing it would make the cost layer depend on the agentic-commerce layer
    and would imply a settlement that did not happen.
    """

    model_config = ConfigDict(extra="allow")

    #: Minor units (cents, satang, …). Non-negative: a refund or credit is not
    #: a negative cost attribution, and a negative here means a caller bug
    #: that would otherwise let a split "conserve" by cancellation.
    amount_minor: int = Field(ge=0)
    #: ISO-4217 alpha-3. Shape-validated only — pinning the full code list
    #: would mean shipping a table that goes stale on every redenomination,
    #: to reject a value NovaFabric only ever records (I-1).
    currency: str

    @field_validator("currency")
    @classmethod
    def _validate_currency(cls, value: str) -> str:
        if len(value) != 3 or not value.isascii() or not value.isupper():
            raise ValueError(
                f"currency {value!r} is not an ISO-4217 alpha-3 code "
                "(three uppercase letters, e.g. 'EUR')"
            )
        return value


# ── Apportionment ─────────────────────────────────────────────────────────


def apportion(total: int, weights: Mapping[str, int]) -> dict[str, int]:
    """Split ``total`` across ``weights``, losing and inventing nothing.

    Uses the largest-remainder (Hamilton) method: each key gets
    ``total * w // Σw``, and the units left over by that flooring are handed
    out one each, to the keys with the largest fractional remainder, ties
    broken by key ascending. The tie-break is what makes the result
    deterministic — 100 across three equal agents must always be
    ``{a: 34, b: 33, c: 33}`` and never a different agent each run, or two
    verifiers re-deriving the same capsule would disagree.

    The remainder is *assigned*, never dropped: ``sum(result.values())``
    equals ``total`` exactly for every input this accepts. Dropping it would
    leave a run total permanently one cent larger than its parts, and the
    conservation check would then fail on arithmetic rather than on the
    attribution error it exists to catch.

    Raises:
        UnapportionableError: on a negative total or weight, or when a
            non-zero total is offered an all-zero key. An all-zero key states
            nothing about who spent what, and splitting evenly over it would
            manufacture a basis the capture never recorded — the caller's
            fail-open move is to leave those agents unattributed (I-3),
            which the facet represents natively.
    """
    if total < 0:
        raise UnapportionableError(f"cannot apportion a negative total ({total})")
    negative = sorted(key for key, weight in weights.items() if weight < 0)
    if negative:
        raise UnapportionableError(
            f"negative weights cannot key an apportionment: {negative}"
        )
    if not weights:
        if total == 0:
            return {}
        raise UnapportionableError(
            f"cannot apportion a non-zero total ({total}) across no agents"
        )

    weight_sum = sum(weights.values())
    if weight_sum == 0:
        if total == 0:
            # Nothing to share out; every agent's share of nothing is nothing.
            # This is a real answer, not a fabricated one.
            return dict.fromkeys(weights, 0)
        raise UnapportionableError(
            f"cannot apportion {total} across an all-zero key; record the "
            "agents as unattributed instead of inventing a share"
        )

    shares = {key: (total * weight) // weight_sum for key, weight in weights.items()}
    # Remainder of `total * weight / weight_sum` for each key, kept as an
    # integer numerator (over the common denominator `weight_sum`) so the
    # ordering is exact — comparing float fractions here would reintroduce
    # the tie-break nondeterminism the docstring promises to avoid.
    remainders = {
        key: (total * weight) % weight_sum for key, weight in weights.items()
    }
    leftover = total - sum(shares.values())
    ranked = sorted(weights, key=lambda key: (-remainders[key], key))
    for key in ranked[:leftover]:
        shares[key] += 1
    return shares


# ── Models ────────────────────────────────────────────────────────────────


class RunTotal(BaseModel):
    """What the run as a whole cost, as already captured.

    Every dimension is optional and independently so: a local run may have
    token counts and no energy receipts, or joules and no pricing catalog.
    An absent dimension is simply not conserved, rather than conserved
    against an assumed zero.
    """

    model_config = ConfigDict(extra="allow")

    cost: Money | None = None
    #: Integer millijoules — see the module note on why not float joules.
    millijoules: int | None = Field(default=None, ge=0)
    tokens_in: int | None = Field(default=None, ge=0)
    tokens_out: int | None = Field(default=None, ge=0)
    calls: int | None = Field(default=None, ge=0)


class AgentCost(BaseModel):
    """One agent's share of the run (spec §4.1).

    Every figure is ``None`` when it was not attributed to this agent — the
    "unattributed" state, which the conservation block accounts for
    explicitly. It is never 0: reporting an unknown cost as zero understates
    a bill and is indistinguishable from an agent that truly spent nothing.
    """

    model_config = ConfigDict(extra="allow")

    agent_id: str
    cost: Money | None = None
    millijoules: int | None = Field(default=None, ge=0)
    tokens_in: int | None = Field(default=None, ge=0)
    tokens_out: int | None = Field(default=None, ge=0)
    calls: int | None = Field(default=None, ge=0)
    basis: Basis
    #: The key an `apportioned` figure was divided by ("token_share", …).
    #: Free text on purpose: the set of defensible keys is open, and an
    #: unrecognised key recorded verbatim is more useful to an auditor than
    #: a closed enum that forces a wrong label.
    apportionment_key: str | None = None


class ConservationCheck(BaseModel):
    """Σ parts == whole, for one dimension (ADR-0146 G5).

    ``ok`` is always ``True`` in a facet produced by :func:`build_facet`,
    because a violation raises instead of being recorded. The field exists
    for the *reading* side: a facet loaded from an untrusted capsule carries
    a claim that :func:`verify_conservation` re-derives, and a tampered
    ``by_agent`` must be catchable without the original builder.

    There is no ``epsilon``: with integer sub-units the identity is exact.
    """

    model_config = ConfigDict(extra="allow")

    dimension: str
    unit: str
    total: int
    #: Sum over agents that carry a figure for this dimension.
    attributed: int
    #: ``total - attributed`` — spend the capture could not tie to any agent.
    #: A positive value is an honest gap, not an error: fail-open (I-3).
    unattributed: int
    ok: bool = True


class CostAttributionFacet(BaseModel):
    """The optional ``facets.cost_attribution`` block (NF-141, I-3)."""

    model_config = ConfigDict(extra="allow")

    schema_version: str = SCHEMA_VERSION
    run_total: RunTotal
    by_agent: list[AgentCost] = Field(default_factory=list)
    conservation: list[ConservationCheck] = Field(default_factory=list)


# ── Facet assembly ────────────────────────────────────────────────────────


def _int_field(model: BaseModel, name: str) -> int | None:
    """Read an optional integer dimension off a model, typed."""
    value: object = getattr(model, name, None)
    # bool is an int subclass; excluded so a stray flag cannot be summed.
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _check_counts(
    run_total: RunTotal, by_agent: list[AgentCost]
) -> list[ConservationCheck]:
    """Conserve every count dimension the run total declares."""
    checks: list[ConservationCheck] = []
    for dimension, unit in _COUNT_DIMENSIONS:
        total = _int_field(run_total, dimension)
        if total is None:
            # Not captured at run level: there is no whole to conserve
            # against. Assuming zero would turn every attributed agent into
            # a conservation failure.
            continue
        attributed = sum(
            value
            for agent in by_agent
            if (value := _int_field(agent, dimension)) is not None
        )
        if attributed > total:
            raise ConservationError(dimension, total, attributed, unit)
        checks.append(
            ConservationCheck(
                dimension=dimension,
                unit=unit,
                total=total,
                attributed=attributed,
                unattributed=total - attributed,
            )
        )
    return checks


def _check_cost(
    run_total: RunTotal, by_agent: list[AgentCost]
) -> ConservationCheck | None:
    """Conserve money, refusing to add amounts in different currencies."""
    total = run_total.cost
    if total is None:
        return None
    attributed = 0
    for agent in by_agent:
        if agent.cost is None:
            continue
        if agent.cost.currency != total.currency:
            raise CurrencyMismatchError(
                f"agent {agent.agent_id!r} is attributed "
                f"{agent.cost.currency} but the run total is "
                f"{total.currency}; cross-currency attribution needs a "
                "declared rate with provenance, which ADR-0146 does not define"
            )
        attributed += agent.cost.amount_minor
    if attributed > total.amount_minor:
        raise ConservationError(
            "cost", total.amount_minor, attributed, f"{total.currency} minor units"
        )
    return ConservationCheck(
        dimension="cost",
        unit=f"{total.currency} minor units",
        total=total.amount_minor,
        attributed=attributed,
        unattributed=total.amount_minor - attributed,
    )


def build_facet(
    run_total: RunTotal, by_agent: Iterable[AgentCost]
) -> CostAttributionFacet | None:
    """Build the cost-attribution facet, or ``None`` when there is no split.

    Fail-open (I-3): a run with no team graph — the single-agent local case —
    yields ``None``, not an exception and not a facet asserting a split that
    was never made. Agents are ordered by ``agent_id`` so the facet is
    byte-identical across runs and re-derivations.

    Raises:
        ConservationError: if the agents account for *more* than the run
            total on any dimension. The numbers are not rescaled to fit.
        CurrencyMismatchError: if agent amounts are not all in the run
            total's currency.
    """
    agents = sorted(by_agent, key=lambda agent: agent.agent_id)
    if not agents:
        return None

    checks: list[ConservationCheck] = []
    cost_check = _check_cost(run_total, agents)
    if cost_check is not None:
        checks.append(cost_check)
    checks.extend(_check_counts(run_total, agents))
    return CostAttributionFacet(
        run_total=run_total, by_agent=agents, conservation=checks
    )


def attach_facet(
    capsule: dict[str, Any], facet: CostAttributionFacet | None
) -> dict[str, Any]:
    """Attach the cost-attribution facet to a capsule dict, additively.

    Writes nothing when there is no facet: a run with nothing to attribute
    must be byte-identical to one captured before this feature existed (I-3).
    Returns a new dict; the input is not mutated.
    """
    if facet is None:
        return capsule
    out = dict(capsule)
    facets = dict(out.get("facets") or {})
    facets[FACET_NAME] = facet.model_dump(exclude_none=True)
    out["facets"] = facets
    return out


# ── Verification ──────────────────────────────────────────────────────────


def verify_conservation(facet: CostAttributionFacet) -> bool:
    """Re-derive every conservation claim from ``by_agent``.

    The reading half of the invariant: :func:`build_facet` cannot be trusted
    to have run over a facet parsed from someone else's capsule. Returns
    ``False`` for a facet whose recorded sums no longer match its agents, or
    that carries ``ok: false``.

    A facet with no conservation block returns ``False``. An unchecked split
    is not "trivially conserving" — it is exactly the facet a verifier exists
    to surface, and returning ``True`` would pass the ones with nothing to
    check.
    """
    if not facet.conservation:
        return False
    for check in facet.conservation:
        if not check.ok or check.total != check.attributed + check.unattributed:
            return False
        if check.dimension == "cost":
            attributed = sum(
                agent.cost.amount_minor
                for agent in facet.by_agent
                if agent.cost is not None
            )
        else:
            attributed = sum(
                value
                for agent in facet.by_agent
                if (value := _int_field(agent, check.dimension)) is not None
            )
        if attributed != check.attributed:
            return False
    return True
