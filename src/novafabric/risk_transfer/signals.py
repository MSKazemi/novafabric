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

"""SLA/warranty-breach and coverage-trigger signals — ADR-0170 P2 (NF-384, NF-386, D4).

Both objects **compare observed facts against declared terms and record the
comparison. They decide nothing.**

- ``risk_transfer.sla_breach`` (NF-384) — ``{sla_ref, metric, operator,
  threshold, observed_value, breach, window, evidence_refs}``. ``operator`` and
  ``threshold`` are the *declared commitment* (e.g. availability ``gte``
  ``99.9``); ``breach`` is ``True`` exactly when the observed value does not
  satisfy it. That is the numeric condition only — no remedy, service credit
  or damages (the counterparty's or court's call), and distinct from the
  ADR-0146 cost showback.
- ``risk_transfer.coverage_trigger`` (NF-386) — ``{covered_event_kind,
  trigger_facts, exclusion_set_ref, matched_exclusions}`` plus optional
  ``parametric_conditions``. ``matched_exclusions`` lists each *declared*
  exclusion whose declared fact markers are present among the observed trigger
  facts; ``parametric_conditions`` compares observed values to *declared*
  parametric trigger thresholds. It records whether the facts are present; it
  **never** decides whether the policy responds, and carries no claim
  decision or payout.

Every threshold and observed value is an exact :class:`~decimal.Decimal`
(never ``float``), and every recorded comparison is re-derived on validation —
a ``breach``/``condition_met`` that disagrees with its own numbers is refused
(:class:`InconsistentComparisonError`), so a stored flag cannot be forged
independently of the figures it summarises.
"""

from __future__ import annotations

import operator as _op
from collections.abc import Callable, Iterable, Mapping
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from novafabric.risk_transfer._guard import (
    LABEL_PATTERN,
    ExactDecimal,
    UnboundedFieldError,
    check_label,
    reject_determination_fields,
    to_decimal,
    validate_ref,
    validate_refs,
)

SIGNALS_SCHEMA_VERSION = "0.1.0"

#: Comparison operators a declared term may use.
ComparisonOperator = Literal["lt", "lte", "gt", "gte", "eq"]

_OPS: dict[str, Callable[[Decimal, Decimal], bool]] = {
    "lt": _op.lt,
    "lte": _op.le,
    "gt": _op.gt,
    "gte": _op.ge,
    "eq": _op.eq,
}

_WINDOW_PATTERN = r"[A-Za-z0-9][A-Za-z0-9 ._:/+-]{0,63}"
_UNIT_PATTERN = r"[A-Za-z0-9%][A-Za-z0-9 %/._-]{0,31}"
_EXCLUSION_ID_PATTERN = r"[A-Za-z0-9][A-Za-z0-9 ._:()/-]{0,63}"

MAX_TRIGGER_FACTS = 256
MAX_EXCLUSIONS = 128
MAX_MARKERS = 32
MAX_CONDITIONS = 32
MAX_EVIDENCE_REFS = 64


class InconsistentComparisonError(Exception):
    """Raised when a recorded ``breach``/``condition_met`` contradicts its figures."""


def compare(observed: Any, operator: str, threshold: Any) -> bool:
    """Return whether ``observed <operator> threshold`` holds, exactly.

    Pure and total over the declared operators. Both sides go through
    :func:`~novafabric.risk_transfer._guard.to_decimal`, so a float is
    refused rather than compared inexactly.

    Raises:
        InvalidDecimalError: a side is not an exact finite decimal.
        ValueError: unknown operator.
    """
    fn = _OPS.get(operator)
    if fn is None:
        raise ValueError(f"operator {operator!r} is not one of {sorted(_OPS)}")
    return fn(to_decimal(observed), to_decimal(threshold))


def _label(value: str, what: str, pattern: str = LABEL_PATTERN) -> str:
    return check_label(value, what=what, pattern=pattern)


# ── NF-384: SLA / warranty breach ─────────────────────────────────────────


class SlaBreach(BaseModel):
    """``risk_transfer.sla_breach`` — observed vs declared threshold (NF-384).

    ``operator``/``threshold`` restate the declared commitment bound by
    ``sla_ref``; ``breach`` is the negation of ``observed <operator>
    threshold`` and is re-derived on validation.
    """

    model_config = ConfigDict(extra="allow")

    schema_version: str = SIGNALS_SCHEMA_VERSION
    #: Digest of the declared SLA/warranty terms document.
    sla_ref: str
    metric: str
    operator: ComparisonOperator
    threshold: ExactDecimal
    observed_value: ExactDecimal
    unit: str | None = None
    breach: bool
    window: str
    evidence_refs: list[str] = Field(default_factory=list)

    @field_validator("sla_ref")
    @classmethod
    def _check_sla_ref(cls, value: str) -> str:
        validate_ref(value)
        return value

    @field_validator("metric")
    @classmethod
    def _check_metric(cls, value: str) -> str:
        return _label(value, "metric")

    @field_validator("unit")
    @classmethod
    def _check_unit(cls, value: str | None) -> str | None:
        return None if value is None else _label(value, "unit", _UNIT_PATTERN)

    @field_validator("window")
    @classmethod
    def _check_window(cls, value: str) -> str:
        return _label(value, "window", _WINDOW_PATTERN)

    @field_validator("evidence_refs")
    @classmethod
    def _check_evidence(cls, value: list[str]) -> list[str]:
        return validate_refs(value, limit=MAX_EVIDENCE_REFS)

    @model_validator(mode="after")
    def _check_consistency(self) -> SlaBreach:
        held = compare(self.observed_value, self.operator, self.threshold)
        if self.breach is held:
            raise InconsistentComparisonError(
                f"sla_breach.breach={self.breach} but observed {self.observed_value} "
                f"{self.operator} {self.threshold} is {held}; breach is re-derived "
                "from the figures and cannot be set independently"
            )
        reject_determination_fields(self.model_dump(mode="json"))
        return self


def build_sla_breach(
    *,
    sla_ref: str,
    metric: str,
    operator: str,
    threshold: Any,
    observed_value: Any,
    window: str,
    unit: str | None = None,
    evidence_refs: Iterable[str] = (),
) -> SlaBreach:
    """Compare an observed value to a declared SLA/warranty threshold (NF-384).

    Computes ``breach`` (the commitment ``observed <operator> threshold`` did
    not hold) and records it with both figures. Records the numeric condition
    only; assesses no remedy, credit or damages.

    Raises:
        InvalidReferenceError, InvalidDecimalError, ValueError,
        DeterminationFieldRejectedError: on bad input.
    """
    held = compare(observed_value, operator, threshold)
    return SlaBreach(
        sla_ref=sla_ref,
        metric=metric,
        operator=operator,  # type: ignore[arg-type]
        threshold=to_decimal(threshold),
        observed_value=to_decimal(observed_value),
        unit=unit,
        breach=not held,
        window=window,
        evidence_refs=list(evidence_refs),
    )


# ── NF-386: coverage trigger ──────────────────────────────────────────────


class TriggerFact(BaseModel):
    """An observed fact offered as evidence of a covered event: digest + marker."""

    model_config = ConfigDict(extra="forbid")

    fact_ref: str
    marker: str

    @field_validator("fact_ref")
    @classmethod
    def _check_ref(cls, value: str) -> str:
        validate_ref(value)
        return value

    @field_validator("marker")
    @classmethod
    def _check_marker(cls, value: str) -> str:
        return _label(value, "fact marker")


class DeclaredExclusion(BaseModel):
    """A declared exclusion endorsement and the fact markers that evidence it."""

    model_config = ConfigDict(extra="forbid")

    exclusion_id: str
    fact_markers: list[str] = Field(min_length=1, max_length=MAX_MARKERS)

    @field_validator("exclusion_id")
    @classmethod
    def _check_id(cls, value: str) -> str:
        return _label(value, "exclusion_id", _EXCLUSION_ID_PATTERN)

    @field_validator("fact_markers")
    @classmethod
    def _check_markers(cls, value: list[str]) -> list[str]:
        return [_label(m, "fact marker") for m in value]


class ParametricTerm(BaseModel):
    """A declared parametric trigger condition: ``metric <operator> threshold``."""

    model_config = ConfigDict(extra="forbid")

    metric: str
    operator: ComparisonOperator
    threshold: ExactDecimal
    unit: str | None = None

    @field_validator("metric")
    @classmethod
    def _check_metric(cls, value: str) -> str:
        return _label(value, "metric")

    @field_validator("unit")
    @classmethod
    def _check_unit(cls, value: str | None) -> str | None:
        return None if value is None else _label(value, "unit", _UNIT_PATTERN)


class MatchedExclusion(BaseModel):
    """A declared exclusion whose declared fact markers were observed."""

    model_config = ConfigDict(extra="allow")

    exclusion_id: str
    matched_markers: list[str]
    fact_refs: list[str]

    @field_validator("exclusion_id")
    @classmethod
    def _check_id(cls, value: str) -> str:
        return _label(value, "exclusion_id", _EXCLUSION_ID_PATTERN)

    @field_validator("matched_markers")
    @classmethod
    def _check_markers(cls, value: list[str]) -> list[str]:
        if len(value) > MAX_MARKERS:
            raise UnboundedFieldError(f"{len(value)} matched markers (cap {MAX_MARKERS})")
        return [_label(m, "fact marker") for m in value]

    @field_validator("fact_refs")
    @classmethod
    def _check_refs(cls, value: list[str]) -> list[str]:
        return validate_refs(value, limit=MAX_TRIGGER_FACTS)


class ParametricCondition(BaseModel):
    """A declared parametric term compared to what was observed.

    ``observed_value``/``condition_met`` are both ``None`` when no observation
    was supplied — *not observed* is recorded as such, never as "not met".
    """

    model_config = ConfigDict(extra="allow")

    metric: str
    operator: ComparisonOperator
    threshold: ExactDecimal
    unit: str | None = None
    observed_value: ExactDecimal | None = None
    condition_met: bool | None = None

    @field_validator("metric")
    @classmethod
    def _check_metric(cls, value: str) -> str:
        return _label(value, "metric")

    @field_validator("unit")
    @classmethod
    def _check_unit(cls, value: str | None) -> str | None:
        return None if value is None else _label(value, "unit", _UNIT_PATTERN)

    @model_validator(mode="after")
    def _check_consistency(self) -> ParametricCondition:
        if self.observed_value is None:
            if self.condition_met is not None:
                raise InconsistentComparisonError(
                    f"parametric condition {self.metric!r} has condition_met with no observed_value"
                )
            return self
        held = compare(self.observed_value, self.operator, self.threshold)
        if self.condition_met is not held:
            raise InconsistentComparisonError(
                f"parametric condition {self.metric!r}: condition_met="
                f"{self.condition_met} but observed {self.observed_value} "
                f"{self.operator} {self.threshold} is {held}"
            )
        return self


class CoverageTrigger(BaseModel):
    """``risk_transfer.coverage_trigger`` — exclusion-aware trigger facts (NF-386).

    Records the facts of a covered event and of declared exclusions; never
    whether the policy responds.
    """

    model_config = ConfigDict(extra="allow")

    schema_version: str = SIGNALS_SCHEMA_VERSION
    covered_event_kind: str
    #: Digests of the capsule signals evidencing the event.
    trigger_facts: list[str] = Field(default_factory=list)
    #: Digest of the declared exclusion set in scope (e.g. ISO CG 40 47).
    exclusion_set_ref: str
    #: Possibly empty. Empty means no declared exclusion's facts were present —
    #: it does **not** mean the policy responds.
    matched_exclusions: list[MatchedExclusion] = Field(default_factory=list)
    parametric_conditions: list[ParametricCondition] | None = None

    @field_validator("covered_event_kind")
    @classmethod
    def _check_kind(cls, value: str) -> str:
        return _label(value, "covered_event_kind")

    @field_validator("trigger_facts")
    @classmethod
    def _check_facts(cls, value: list[str]) -> list[str]:
        return validate_refs(value, limit=MAX_TRIGGER_FACTS)

    @field_validator("exclusion_set_ref")
    @classmethod
    def _check_set_ref(cls, value: str) -> str:
        validate_ref(value)
        return value

    @field_validator("matched_exclusions")
    @classmethod
    def _check_matches(cls, value: list[MatchedExclusion]) -> list[MatchedExclusion]:
        if len(value) > MAX_EXCLUSIONS:
            raise UnboundedFieldError(f"{len(value)} matched exclusions (cap {MAX_EXCLUSIONS})")
        return value

    @field_validator("parametric_conditions")
    @classmethod
    def _check_conditions(
        cls, value: list[ParametricCondition] | None
    ) -> list[ParametricCondition] | None:
        if value is not None and len(value) > MAX_CONDITIONS:
            raise UnboundedFieldError(f"{len(value)} parametric conditions (cap {MAX_CONDITIONS})")
        return value

    @model_validator(mode="after")
    def _check_matches_are_facts(self) -> CoverageTrigger:
        facts = set(self.trigger_facts)
        for match in self.matched_exclusions:
            stray = [ref for ref in match.fact_refs if ref not in facts]
            if stray or not match.fact_refs:
                raise InconsistentComparisonError(
                    f"matched exclusion {match.exclusion_id!r} must cite only "
                    "recorded trigger_facts (and at least one)"
                )
        reject_determination_fields(self.model_dump(mode="json"))
        return self


def _as_model(value: Any, model: type[Any]) -> Any:
    return value if isinstance(value, model) else model.model_validate(value)


def build_coverage_trigger(
    *,
    covered_event_kind: str,
    exclusion_set_ref: str,
    trigger_facts: Iterable[TriggerFact | Mapping[str, Any]] = (),
    declared_exclusions: Iterable[DeclaredExclusion | Mapping[str, Any]] = (),
    parametric_terms: Iterable[ParametricTerm | Mapping[str, Any]] = (),
    observations: Mapping[str, Any] | None = None,
) -> CoverageTrigger:
    """Record which declared exclusions' facts, and which parametric conditions, are present.

    Mechanical comparison only: a declared exclusion is *matched* when at
    least one of its declared ``fact_markers`` appears among the observed
    trigger facts' markers; the matching fact digests are cited. A parametric
    term is compared to ``observations[metric]`` when supplied and recorded as
    *not observed* otherwise. Output order is deterministic (declared order
    for exclusions and terms, first-seen order for facts, de-duplicated).

    Decides nothing: neither the presence of a covered event's facts nor an
    empty ``matched_exclusions`` is a statement that the policy responds.

    Raises:
        InvalidReferenceError, InvalidDecimalError, UnboundedFieldError,
        ValueError, DeterminationFieldRejectedError: on bad input.
    """
    facts = [_as_model(f, TriggerFact) for f in trigger_facts]
    if len(facts) > MAX_TRIGGER_FACTS:
        raise UnboundedFieldError(f"{len(facts)} trigger facts (cap {MAX_TRIGGER_FACTS})")
    exclusions = [_as_model(e, DeclaredExclusion) for e in declared_exclusions]
    if len(exclusions) > MAX_EXCLUSIONS:
        raise UnboundedFieldError(f"{len(exclusions)} exclusions (cap {MAX_EXCLUSIONS})")
    terms = [_as_model(t, ParametricTerm) for t in parametric_terms]
    if len(terms) > MAX_CONDITIONS:
        raise UnboundedFieldError(f"{len(terms)} parametric terms (cap {MAX_CONDITIONS})")
    obs = dict(observations or {})

    fact_refs: list[str] = []
    for fact in facts:
        if fact.fact_ref not in fact_refs:
            fact_refs.append(fact.fact_ref)

    matched: list[MatchedExclusion] = []
    for exclusion in exclusions:
        wanted = set(exclusion.fact_markers)
        hits = [f for f in facts if f.marker in wanted]
        if not hits:
            continue
        markers = [m for m in exclusion.fact_markers if any(f.marker == m for f in hits)]
        refs: list[str] = []
        for fact in hits:
            if fact.fact_ref not in refs:
                refs.append(fact.fact_ref)
        matched.append(
            MatchedExclusion(
                exclusion_id=exclusion.exclusion_id, matched_markers=markers, fact_refs=refs
            )
        )

    conditions: list[ParametricCondition] = []
    for term in terms:
        if term.metric in obs and obs[term.metric] is not None:
            observed = to_decimal(obs[term.metric])
            met: bool | None = compare(observed, term.operator, term.threshold)
        else:
            observed, met = None, None
        conditions.append(
            ParametricCondition(
                metric=term.metric,
                operator=term.operator,
                threshold=term.threshold,
                unit=term.unit,
                observed_value=observed,
                condition_met=met,
            )
        )

    return CoverageTrigger(
        covered_event_kind=covered_event_kind,
        trigger_facts=fact_refs,
        exclusion_set_ref=exclusion_set_ref,
        matched_exclusions=matched,
        parametric_conditions=conditions or None,
    )


__all__ = [
    "SIGNALS_SCHEMA_VERSION",
    "ComparisonOperator",
    "CoverageTrigger",
    "DeclaredExclusion",
    "InconsistentComparisonError",
    "MatchedExclusion",
    "ParametricCondition",
    "ParametricTerm",
    "SlaBreach",
    "TriggerFact",
    "build_coverage_trigger",
    "build_sla_breach",
    "compare",
]
