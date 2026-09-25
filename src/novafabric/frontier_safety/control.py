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

"""AI-control-protocol decisions and tripwire triggers — ADR-0167 D2/P2.

Two record-only evidence objects for the ``facets.frontier_safety`` block:

- :class:`ControlDecision` (NF-352) — the outcome an **external** AI-control
  protocol (trusted/untrusted monitoring, Ctrl-Z resampling, defer-to-trusted,
  defer-to-human, interrogation) produced for one run action. It is linked to
  that action by ``governed_action_ref`` and to the monitor that decided by
  ``monitor_ref`` — a digest of the monitor's model/config identity, never its
  prompt.
- :class:`TripwireTrigger` (NF-357) — that a **published** framework indicator
  fired, bound by ``commitment_ref`` to the NF-353 commitment it implicates.

What this module does **not** do is the point of it (ADR-0167 I-1):

- It never runs a control protocol, never monitors, never resamples.
- It never applies the safeguard a tripwire implies, and never blocks, gates,
  pauses or refuses the workload. A ``decision: "defer"`` recorded here is a
  sentence about what the external protocol did, not an instruction to anyone.
- It never computes a verdict (I-3). A control decision is *attributed*
  evidence: ``verdict_source`` is fixed to ``control_protocol`` and
  ``verdict_ref`` — the protocol's own decision-log entry — is required, so a
  sealed decision always names who made it.

Straddling events (ADR-0167 D6): when the monitor that decided also acted as
a C4 input/output guardrail (ADR-0145, ``facets.safety``), the C4 decision is
referenced by digest via ``guardrail_decision_ref`` — see
:func:`guardrail_decision_digest` — and its fields are *rejected* if inlined
(:class:`~novafabric.frontier_safety._common.GuardrailDuplicationError`).
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    ValidationInfo,
    field_validator,
    model_validator,
)

from novafabric.frontier_safety._common import (
    SCHEMA_VERSION,
    Framework,
    _ExternalVerdict,
    _validate_digest,
    check_extra_fields,
    digest_ref,
)

if TYPE_CHECKING:  # pragma: no cover - typing only; avoids an import cycle
    from novafabric.frontier_safety.facet import CommitmentBinding

__all__ = [
    "ControlDecision",
    "ControlOutcome",
    "ControlProtocol",
    "TripwireTrigger",
    "decisions_for_action",
    "guardrail_decision_digest",
    "verify_governed_action",
    "verify_guardrail_link",
    "verify_tripwire_commitment",
]

#: The AI-control protocol families the spec names (§3.5, after Redwood
#: Research's control work). ``other`` keeps a lab-specific protocol on the
#: record rather than dropping the evidence.
ControlProtocol = Literal[
    "trusted_monitor",
    "untrusted_monitor",
    "resample",
    "defer_to_trusted",
    "defer_to_human",
    "interrogate",
    "other",
]

#: The outcome the external protocol produced for the governed action. Past
#: tense in meaning: this is what *was* decided elsewhere, recorded here.
ControlOutcome = Literal["accept", "edit", "resample", "defer", "audit"]


def _non_empty(value: str, *, field: str) -> str:
    """Return ``value`` unless it is blank, which would make it unmatchable."""
    if not value.strip():
        raise ValueError(f"{field} must be non-empty (ADR-0167 P2)")
    return value


class ControlDecision(_ExternalVerdict):
    """One external AI-control-protocol decision about a run action (NF-352).

    Records *that* an external protocol reached ``decision`` about the action
    identified by ``governed_action_ref``. NovaFabric ran no protocol and made
    no decision; the decision is attributed evidence, so ``verdict_ref`` (the
    protocol's decision-log entry, digest or URI) is required and
    ``verdict_source`` can only be ``control_protocol``.
    """

    model_config = ConfigDict(extra="allow")

    schema_version: str = SCHEMA_VERSION
    protocol: ControlProtocol
    decision: ControlOutcome
    #: Digest of the run action the protocol governed — the governed-action
    #: linkage. Content identity, not a locator: the action a decision was
    #: about must not be re-pointable after sealing.
    governed_action_ref: str
    #: Digest of the monitoring model/config identity. Never its prompt (I-5):
    #: a prompt-shaped extra field is rejected by :meth:`_check_shape`.
    monitor_ref: str
    #: Fixed: a control decision's only permissible source is the protocol.
    verdict_source: Literal["control_protocol"] = "control_protocol"
    #: Required, unlike on the P1 objects: the decision *is* the protocol's
    #: output, so it is only recordable together with a pointer to it.
    verdict_ref: str
    #: C4 straddle (ADR-0167 D6): digest of the ``facets.safety`` guardrail
    #: decision the same monitor produced, if any.
    guardrail_decision_ref: str | None = None

    @field_validator("governed_action_ref", "monitor_ref", mode="before")
    @classmethod
    def _check_digests(cls, v: object, info: ValidationInfo) -> str:
        return _validate_digest(v, field=str(info.field_name))

    @field_validator("guardrail_decision_ref", mode="before")
    @classmethod
    def _check_guardrail_ref(cls, v: object) -> str | None:
        if v is None:
            return None
        return _validate_digest(v, field="guardrail_decision_ref")

    @model_validator(mode="after")
    def _check_shape(self) -> ControlDecision:
        check_extra_fields(self.model_extra, owner=type(self).__name__)
        return self


class TripwireTrigger(_ExternalVerdict):
    """A published framework indicator that fired (NF-357).

    Records **that** ``indicator_id`` fired, **what** observed it
    (``triggered_by_ref``) and **which** NF-353 commitment it implicates
    (``commitment_ref``). It never applies the safeguard the commitment
    prescribes and never blocks anything (I-1). ``verdict_ref`` is optional:
    an indicator can fire before any evaluator has ruled on what it means, and
    that honest "not evaluated yet" state is ``verdict: null`` (I-4).
    """

    model_config = ConfigDict(extra="allow")

    schema_version: str = SCHEMA_VERSION
    #: The published indicator id, e.g. ``fsf.ccl.cyber.autonomous-uplift``.
    indicator_id: str
    framework: Framework
    #: Digest of the NF-353 published commitment this trigger implicates —
    #: the same value as that binding's ``commitment_digest``.
    commitment_ref: str
    #: Literal True: the object means "this fired". An indicator that did not
    #: fire is recorded by there being no trigger at all (I-2), never by a
    #: ``fired: false`` object that a truthiness check could misread.
    fired: Literal[True] = True
    #: Digest of the observing event or eval that tripped the indicator.
    triggered_by_ref: str
    guardrail_decision_ref: str | None = None

    @field_validator("indicator_id")
    @classmethod
    def _check_indicator(cls, v: str) -> str:
        return _non_empty(v, field="indicator_id")

    @field_validator("commitment_ref", "triggered_by_ref", mode="before")
    @classmethod
    def _check_digests(cls, v: object, info: ValidationInfo) -> str:
        return _validate_digest(v, field=str(info.field_name))

    @field_validator("guardrail_decision_ref", mode="before")
    @classmethod
    def _check_guardrail_ref(cls, v: object) -> str | None:
        if v is None:
            return None
        return _validate_digest(v, field="guardrail_decision_ref")

    @model_validator(mode="after")
    def _check_shape(self) -> TripwireTrigger:
        check_extra_fields(self.model_extra, owner=type(self).__name__)
        return self


# ── Linkage & verification (offline, content-only) ────────────────────────


def guardrail_decision_digest(decision: BaseModel | Mapping[str, Any]) -> str:
    """Return the ``sha256:`` reference for a C4 guardrail-decision object.

    Canonical JSON (sorted keys, compact separators, ``None`` fields dropped)
    so the same decision always digests the same way regardless of field
    order or which optional fields a producer happened to serialise as null.
    Accepts the Pydantic ``GuardrailDecision`` from ``novafabric.safety`` or its
    serialised mapping, without importing the C4 package — the boundary is a
    digest, not a code dependency.
    """
    if isinstance(decision, BaseModel):
        data: Any = decision.model_dump(mode="json", exclude_none=True)
    else:
        data = {k: v for k, v in decision.items() if v is not None}
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)
    return digest_ref(canonical)


def verify_guardrail_link(
    record: ControlDecision | TripwireTrigger,
    decision: BaseModel | Mapping[str, Any],
) -> bool:
    """True when ``record`` references exactly this C4 guardrail decision.

    False — never an exception — when the record carries no reference or
    names a different decision: a missing straddle link is a finding to
    report, not a crash.
    """
    ref = record.guardrail_decision_ref
    return ref is not None and ref == guardrail_decision_digest(decision)


def verify_governed_action(decision: ControlDecision, action_record: str | bytes) -> bool:
    """True when ``decision`` governed exactly this action record.

    The governed-action linkage check: the caller hashes the action as the
    capsule recorded it and this confirms the decision is about that action
    and not another one.
    """
    return decision.governed_action_ref == digest_ref(action_record)


def verify_tripwire_commitment(trigger: TripwireTrigger, binding: CommitmentBinding) -> bool:
    """True when ``trigger`` implicates the commitment ``binding`` names.

    The incident→commitment direction of NF-353 for a fired indicator: the
    trigger's ``commitment_ref`` must equal the binding's
    ``commitment_digest``. Says *which* commitment is implicated, never
    whether it was satisfied.
    """
    return trigger.commitment_ref == binding.commitment_digest


def decisions_for_action(
    decisions: Iterable[ControlDecision], action_ref: str
) -> tuple[ControlDecision, ...]:
    """Every decision whose ``governed_action_ref`` is ``action_ref``.

    Order is preserved; a resampled action typically carries several.
    """
    return tuple(d for d in decisions if d.governed_action_ref == action_ref)
