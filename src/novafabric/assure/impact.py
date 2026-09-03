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

"""Model-update impact report — ADR-0147 D3 / NF-154.

Aggregates per-run **C3 equivalence verdicts** for a corpus of pinned baselines
replayed through a substituted model binding, into one report for
``from_model`` → ``to_model``.

**It decides nothing.** ADR-0147 is explicit that NF-154 *"MUST NOT decide whether
to adopt the new model"*, so there is no ``recommendation``, ``adopt`` or ``pass``
field — a field with that name would be the decision, whatever the surrounding
prose said. The report states what changed; a human decides what to do about it.

**It scores nothing either.** Equivalence comes from C3 (ADR-0144), exposed as
``nova replay-equivalence check``. One verdict engine, many consumers.

Three things here are easy to get quietly wrong, and each is guarded:

- **A run with no cost data is not a run that cost zero.** Summing a missing cost
  as ``0`` produces a delta that looks precise and is wrong, so the report records
  how many runs actually contributed to each delta.
- **Currencies are not interchangeable.** Adding EUR minor units to JPY minor units
  yields a number with no meaning, so a mixed corpus is refused rather than summed.
- **Every run is accounted for.** ``equivalent + regressed + inconclusive == n`` is
  an enforced identity. A baseline that could not be replayed is *inconclusive* —
  never dropped, and never quietly counted as a pass.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from novafabric.cost.attribution import Money

SCHEMA_VERSION = "0.1.0"
FACET_NAME = "impact_report"

#: How many regressions the report lists by default.
DEFAULT_WORST_N = 5


class ImpactError(ValueError):
    """An impact report could not be built."""


class RunOutcome(BaseModel):
    """One baseline replayed through the substituted model.

    ``equivalent`` is the C3 verdict. ``None`` means the run could not be
    replayed or judged — recorded as inconclusive, never as a pass.
    """

    model_config = ConfigDict(extra="allow", frozen=True)

    baseline_id: str
    equivalent: bool | None = None
    #: C3 distance; 0.0 identical, 1.0 maximally different.
    distance: float | None = None
    cost_before: Money | None = None
    cost_after: Money | None = None
    tokens_before: int | None = None
    tokens_after: int | None = None

    @property
    def inconclusive(self) -> bool:
        return self.equivalent is None

    @property
    def regressed(self) -> bool:
        return self.equivalent is False


class Delta(BaseModel):
    """A signed change, with how many runs it was computed from.

    Not a :class:`Money`: that type is ``ge=0`` by design, and a model update can
    legitimately make a run *cheaper*. ``contributing_runs`` is what keeps the
    number honest — a delta summed over 3 of 40 runs is a different claim from one
    summed over all 40, and without the count the two are indistinguishable.
    """

    model_config = ConfigDict(extra="allow", frozen=True)

    #: Signed. Negative means the substituted model was cheaper / used fewer tokens.
    amount: int
    #: ISO-4217 for a cost delta; None for a token delta, which is dimensionless.
    currency: str | None = None
    contributing_runs: int
    #: Runs that carried no data for this dimension, so contributed nothing.
    missing_runs: int


class Regression(BaseModel):
    """One regressed run, for the ``worst_regressions`` list."""

    model_config = ConfigDict(extra="allow", frozen=True)

    baseline_id: str
    distance: float | None = None


class ImpactReport(BaseModel):
    """The aggregate for ``from_model`` → ``to_model`` (NF-154)."""

    model_config = ConfigDict(extra="allow", frozen=True)

    schema_version: str = SCHEMA_VERSION
    from_model: str
    to_model: str
    n: int
    equivalent: int
    regressed: int
    #: Runs whose verdict is unknown. Additive to the spec's field list: without it
    #: the counts do not sum to ``n`` and the reader cannot tell why.
    inconclusive: int
    cost_delta: Delta
    token_delta: Delta
    worst_regressions: list[Regression] = Field(default_factory=list)

    @model_validator(mode="after")
    def _counts_are_conserved(self) -> ImpactReport:
        total = self.equivalent + self.regressed + self.inconclusive
        if total != self.n:
            raise ValueError(
                f"counts do not conserve: equivalent({self.equivalent}) + "
                f"regressed({self.regressed}) + inconclusive({self.inconclusive}) "
                f"= {total}, expected n={self.n}. A run has been dropped or "
                "double-counted."
            )
        return self


def _sum_currency(outcomes: list[RunOutcome]) -> str | None:
    """The single currency of the corpus, or raise if they disagree."""
    seen = {
        m.currency
        for o in outcomes
        for m in (o.cost_before, o.cost_after)
        if m is not None
    }
    if not seen:
        return None
    if len(seen) > 1:
        raise ImpactError(
            "corpus mixes currencies "
            f"({', '.join(sorted(seen))}); minor units from different currencies "
            "cannot be summed into one delta"
        )
    return seen.pop()


def _cost_delta(outcomes: list[RunOutcome], currency: str | None) -> Delta:
    contributing = [
        o for o in outcomes if o.cost_before is not None and o.cost_after is not None
    ]
    amount = 0
    for outcome in contributing:
        assert outcome.cost_before is not None and outcome.cost_after is not None
        amount += outcome.cost_after.amount_minor - outcome.cost_before.amount_minor
    return Delta(
        amount=amount,
        currency=currency,
        contributing_runs=len(contributing),
        missing_runs=len(outcomes) - len(contributing),
    )


def _token_delta(outcomes: list[RunOutcome]) -> Delta:
    contributing = [
        o for o in outcomes
        if o.tokens_before is not None and o.tokens_after is not None
    ]
    amount = 0
    for outcome in contributing:
        assert outcome.tokens_before is not None and outcome.tokens_after is not None
        amount += outcome.tokens_after - outcome.tokens_before
    return Delta(
        amount=amount,
        currency=None,
        contributing_runs=len(contributing),
        missing_runs=len(outcomes) - len(contributing),
    )


def build_report(
    outcomes: list[RunOutcome],
    *,
    from_model: str,
    to_model: str,
    worst_n: int = DEFAULT_WORST_N,
) -> ImpactReport:
    """Aggregate per-run C3 verdicts into the impact report.

    Raises:
        ImpactError: on an empty corpus, or one mixing currencies.
    """
    if not outcomes:
        raise ImpactError(
            "an impact report needs at least one run; an empty corpus would "
            "report 0 regressions, which reads as a clean result"
        )
    if worst_n < 0:
        raise ImpactError("worst_n cannot be negative")

    currency = _sum_currency(outcomes)

    regressions = sorted(
        (o for o in outcomes if o.regressed),
        key=lambda o: (-(o.distance if o.distance is not None else 0.0), o.baseline_id),
    )

    return ImpactReport(
        from_model=from_model,
        to_model=to_model,
        n=len(outcomes),
        equivalent=sum(1 for o in outcomes if o.equivalent is True),
        regressed=len(regressions),
        inconclusive=sum(1 for o in outcomes if o.inconclusive),
        cost_delta=_cost_delta(outcomes, currency),
        token_delta=_token_delta(outcomes),
        worst_regressions=[
            Regression(baseline_id=o.baseline_id, distance=o.distance)
            for o in regressions[:worst_n]
        ],
    )


# ── Capsule facet ─────────────────────────────────────────────────────────


def attach_facet(
    capsule: dict[str, Any], report: ImpactReport | None
) -> dict[str, Any]:
    """Attach the report additively; returns a new dict."""
    if report is None:
        return capsule
    out = dict(capsule)
    facets = dict(out.get("facets") or {})
    facets[FACET_NAME] = report.model_dump(exclude_none=True)
    out["facets"] = facets
    return out


def facet_from_capsule(capsule: dict[str, Any]) -> ImpactReport | None:
    """Read the report back out of a capsule dict, or None if absent."""
    facets = capsule.get("facets")
    if not isinstance(facets, dict):
        return None
    block = facets.get(FACET_NAME)
    if not isinstance(block, dict):
        return None
    try:
        return ImpactReport.model_validate(block)
    except ValueError as exc:
        raise ImpactError(f"capsule holds an invalid impact report: {exc}") from exc
