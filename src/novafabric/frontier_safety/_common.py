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

"""Shared building blocks for the frontier-safety facet objects (ADR-0167).

Every frontier-safety object — the P1 threshold eval and commitment binding
(:mod:`novafabric.frontier_safety.facet`) and the P2 control-protocol decision
and tripwire trigger (:mod:`novafabric.frontier_safety.control`) — shares the
same reference grammar, the same named errors and the same verdict invariant
(I-3). They live here so each object module can import them without an import
cycle through the facet root that aggregates those objects.

Private module: the public names are re-exported from
:mod:`novafabric.frontier_safety` (and, for backward compatibility, from
:mod:`novafabric.frontier_safety.facet`).
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

SCHEMA_VERSION = "0.1.0"

#: The in-mission-boundary line every ``nova safety …`` output prints (spec
#: §3.16). One constant so the CLI, and later the P5 verifier, cannot drift
#: into slightly different — and slightly weaker — wordings.
IN_MISSION_BOUNDARY = (
    "Record-only: NovaFabric records external frontier-safety decisions by "
    "reference; it never runs a control protocol, never computes a verdict, "
    "and never blocks or gates the workload (fail-open, ADR-0167)."
)

#: The published frontier-safety frameworks this facet can bind to (spec §3.4).
#: `other` exists so a lab with its own framework is recorded rather than
#: dropped — dropping it would lose the evidence this cluster exists to keep.
Framework = Literal["anthropic_rsp", "openai_preparedness", "deepmind_fsf", "other"]

#: Who issued an external verdict (spec §3.2). There is deliberately no
#: `novafabric` member: a verdict NovaFabric authored is exactly what I-3
#: forbids, and leaving the value unspellable is stronger than rejecting it.
VerdictSource = Literal[
    "rsp_evaluator",
    "preparedness_evaluator",
    "fsf_evaluator",
    "control_protocol",
    "red_team",
    "scheming_eval",
    "human_decision",
]


#: The one digest form the rest of the capsule uses. Matched strictly
#: (lower-case hex, exact length) so a truncated or upper-cased digest fails
#: loudly here rather than failing to match at verify time, months later, in
#: an audit. Deliberately identical to the ADR-0152 shape rather than imported
#: from it: an auditor should see one digest form across all facets, and this
#: cluster should not acquire a dependency on the model-provenance cluster to
#: get it.
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

#: An external artifact may also be named by locator — an https URL, an
#: evaluator's report URI. A URI *shape* only; nothing here dereferences it
#: (offline by default, and I-2's fail-open rule forbids a network call on the
#: capture path).
_URI_RE = re.compile(r"^[a-z][a-z0-9+.\-]*://\S+$")

#: A reference is an identifier, never a document. Anything longer is
#: overwhelmingly likely to be inlined content someone tried to smuggle
#: through a "ref" field, which is exactly what I-5 exists to stop.
MAX_REF_LENGTH = 2048


# ── Errors ────────────────────────────────────────────────────────────────


class FrontierSafetyError(Exception):
    """Base for every error this module raises.

    Subclasses :class:`Exception`, not :class:`ValueError`: these are
    invariant violations of a safety-evidence contract, and a caller wrapping
    a broad ``except ValueError`` around capsule assembly must not swallow
    them by accident.
    """


class ComputedVerdictError(FrontierSafetyError):
    """Raised when an object carries a verdict NovaFabric would be authoring.

    The single most consequential error available in this module is for
    NovaFabric to appear to certify a model as safe. A verdict value with no
    external attribution is indistinguishable, once sealed, from a NovaFabric
    judgement — so it is rejected at construction (ADR-0167 D4, I-3).
    """


class InvalidReferenceError(FrontierSafetyError):
    """Raised when a reference is neither a ``sha256:`` digest nor a URI."""


class GuardrailDuplicationError(FrontierSafetyError):
    """Raised when a frontier-safety object re-records a C4 guardrail decision.

    ADR-0167 D6 / spec §3.15: the input/output guardrail layer (ADR-0145,
    NF-131..140, ``facets.safety``) owns guardrail decisions. A straddling
    event — say, a control-protocol monitor that also acted as a guardrail —
    references the C4 object by digest through ``guardrail_decision_ref``. It
    never carries the C4 object's fields inline: two copies of one decision in
    two facets can drift, and an auditor then cannot tell which is sealed
    truth.
    """


class PayloadCaptureError(FrontierSafetyError):
    """Raised when something payload-shaped is passed where a reference belongs.

    Distinct from :class:`InvalidReferenceError` on purpose: a malformed
    digest is a caller mistake, while a payload arriving at this boundary is
    an I-5 violation, and the two want different fixes from whoever reads the
    traceback.
    """


# ── Reference validation ──────────────────────────────────────────────────


def _validate_ref(value: object, *, field: str) -> str:
    """Return ``value`` if it is an acceptable external-artifact reference.

    Raises:
        PayloadCaptureError: if ``value`` is bytes, or is too long to be an
            identifier. Bytes are rejected rather than hashed for the caller:
            hashing here would make it effortless to hand this module a
            red-team transcript and have it quietly do the right-looking
            thing, with no trace that the bytes were ever in the process.
        InvalidReferenceError: if ``value`` is neither ``sha256:<hex>`` nor a
            URI.
    """
    if isinstance(value, (bytes, bytearray, memoryview)):
        raise PayloadCaptureError(
            f"{field} must be a reference (sha256 digest or URI), not raw bytes; "
            "digest the artifact yourself with digest_ref() and pass the result "
            "(ADR-0167 I-5 — the capsule never holds eval results, exploit "
            "payloads or red-team transcripts)"
        )
    if not isinstance(value, str):
        raise InvalidReferenceError(f"{field} must be a string reference")
    if len(value) > MAX_REF_LENGTH:
        raise PayloadCaptureError(
            f"{field} is {len(value)} chars, over the {MAX_REF_LENGTH}-char "
            "reference limit; this looks like inlined content, not a ref"
        )
    if not (_DIGEST_RE.match(value) or _URI_RE.match(value)):
        raise InvalidReferenceError(f"{field} must be 'sha256:<64 hex>' or a URI, got {value!r}")
    return value


def _validate_digest(value: object, *, field: str) -> str:
    """Return ``value`` if it is a ``sha256:`` digest.

    Stricter than :func:`_validate_ref`: some fields bind by *content
    identity* and a URI would not be a binding at all — it names where
    something lives, not what it is, and the thing it names can be edited
    underneath the capsule. A published framework commitment is exactly such a
    field: "the commitment we were held to" must not be a URL whose text can
    change after the fact.
    """
    ref = _validate_ref(value, field=field)
    if not _DIGEST_RE.match(ref):
        raise InvalidReferenceError(
            f"{field} must be a content digest 'sha256:<64 hex>', not a locator; got {ref!r}"
        )
    return ref


def digest_ref(artifact: str | bytes) -> str:
    """Return the ``sha256:`` reference for an external safety artifact.

    Callers hash the artifact — an NF-154 eval-integrity record, a published
    commitment section, an evaluator's verdict document — and put only the
    result in the capsule. Emitted in the same ``sha256:<hex>`` form the rest
    of the capsule uses, so a verifier does not have to know which subsystem
    wrote it.
    """
    raw = artifact.encode("utf-8") if isinstance(artifact, str) else artifact
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


# ── The verdict invariant (I-3) ───────────────────────────────────────────


class _ExternalVerdict(BaseModel):
    """Mixin for every object that may record an external safety verdict.

    ADR-0167 D4 rejects "a non-null *computed* verdict". What makes a verdict
    non-computed is attribution: ``verdict_ref`` (the external verdict
    document) plus ``verdict_source`` (who issued it). So the permitted shapes
    are exactly two, and the model validator below enforces them:

    1. ``verdict is None`` — NovaFabric observed an event and forms no
       judgement. This is the canonical shape and the default.
    2. ``verdict`` is set **and** both ``verdict_ref`` and ``verdict_source``
       are set — NovaFabric is quoting a named external evaluator.

    Anything else raises. A verdict with no ref, or with a ref but no named
    source, reads to a downstream consumer as NovaFabric's own determination
    once it is sealed into the root — which is the failure mode I-3 exists to
    make impossible.
    """

    model_config = ConfigDict(extra="allow")

    #: Typed ``Any`` rather than ``None``: a field typed ``None`` would reject
    #: a non-null verdict with Pydantic's generic "input should be null", and
    #: the caller would learn nothing about *why* NovaFabric refuses to hold a
    #: verdict. The validator below raises a named error that explains it.
    verdict: Any = None
    #: Digest/URI of the EXTERNAL verdict document. Recording the reference
    #: with ``verdict: null`` is the shape the spec §4.1 example uses: the
    #: judgement lives behind the ref, not in the capsule.
    verdict_ref: str | None = None
    verdict_source: VerdictSource | None = None

    @field_validator("verdict_ref", mode="before")
    @classmethod
    def _check_verdict_ref(cls, v: object) -> str | None:
        if v is None:
            return None
        return _validate_ref(v, field="verdict_ref")

    @model_validator(mode="after")
    def _check_verdict_is_never_ours(self) -> _ExternalVerdict:
        if self.verdict is None:
            # "Not evaluated" — never "safe", never "unsafe" (I-4). A verdict
            # ref may still be present without a verdict value; that is the
            # canonical verdict-by-reference shape, not an error.
            return self
        if self.verdict_ref is None:
            raise ComputedVerdictError(
                f"{type(self).__name__} carries verdict={self.verdict!r} with no "
                "verdict_ref; NovaFabric never authors a frontier-safety verdict. "
                "Either leave verdict null (observed, no judgement) or record the "
                "external evaluator's verdict_ref + verdict_source (ADR-0167 D4)"
            )
        if self.verdict_source is None:
            raise ComputedVerdictError(
                f"{type(self).__name__} carries verdict={self.verdict!r} with a "
                "verdict_ref but no verdict_source; an unattributed verdict is "
                "indistinguishable from a NovaFabric judgement once sealed "
                "(ADR-0167 D4)"
            )
        return self

    @property
    def is_evaluated(self) -> bool:
        """True only when an external verdict was actually recorded.

        Deliberately not named ``is_safe`` or ``passed``. This answers "did an
        external evaluator reach a conclusion we can point at", never "was the
        outcome good" — NovaFabric does not know and must not imply (I-4).
        """
        return self.verdict is not None or self.verdict_ref is not None


# ── Shape guards shared by the P2+ objects (I-5, C4 boundary) ─────────────

#: Key names that mean "a payload was inlined here". Matched case-insensitively
#: against every key an object carries beyond its declared fields — the
#: ``extra="allow"`` surface is exactly where a producer would smuggle a monitor
#: prompt or an exploit transcript, so that is where the check has to look.
PAYLOAD_KEYS: frozenset[str] = frozenset(
    {
        "prompt",
        "prompts",
        "system_prompt",
        "monitor_prompt",
        "completion",
        "response_text",
        "transcript",
        "red_team_transcript",
        "payload",
        "exploit",
        "exploit_payload",
        "exploit_steps",
        "weights",
        "model_weights",
        "activations",
        "raw_output",
        "raw_input",
    }
)

#: Field names that only a C4 guardrail-decision object (ADR-0145,
#: ``novafabric.safety.decisions.GuardrailDecision``) carries. Their presence
#: on a frontier-safety object means the guardrail decision is being
#: re-recorded rather than referenced (spec §3.15).
C4_INLINE_KEYS: frozenset[str] = frozenset(
    {
        "guardrail_decision",
        "guardrail_decisions",
        "disposition",
        "decision_inputs_digest",
        "injection_attempt",
        "jailbreak_attempt",
    }
)

#: Nesting bound for the extra-field walk. Recursion over caller-supplied data
#: is bounded (CLAUDE.md code style); anything nested deeper than this is
#: rejected as a payload rather than walked.
MAX_EXTRA_DEPTH = 8


def _iter_keys(value: object, *, depth: int = 0) -> list[str]:
    """Return every mapping key nested inside ``value``, bounded by depth.

    Raises:
        PayloadCaptureError: if the structure nests deeper than
            :data:`MAX_EXTRA_DEPTH`; a reference record is flat, and deep
            nesting is overwhelmingly an inlined document.
    """
    if depth > MAX_EXTRA_DEPTH:
        raise PayloadCaptureError(
            f"extra fields nest deeper than {MAX_EXTRA_DEPTH} levels; a "
            "frontier-safety record holds references, not documents (I-5)"
        )
    keys: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            keys.append(str(key))
            keys.extend(_iter_keys(item, depth=depth + 1))
    elif isinstance(value, (list, tuple)):
        for item in value:
            keys.extend(_iter_keys(item, depth=depth + 1))
    return keys


def check_extra_fields(extra: dict[str, Any] | None, *, owner: str) -> None:
    """Reject payload-shaped or C4-duplicating keys in an object's extra fields.

    Args:
        extra: the object's ``model_extra`` (undeclared fields kept by
            ``extra="allow"``). ``None`` or empty passes.
        owner: the object's class name, for the error message.

    Raises:
        PayloadCaptureError: a key names a prompt, transcript, payload, weights
            or activations (I-5, ADR-0009, ADR-0021 §4).
        GuardrailDuplicationError: a key belongs to a C4 guardrail-decision
            object; reference it via ``guardrail_decision_ref`` instead.
    """
    if not extra:
        return
    keys = {k.lower() for k in _iter_keys(extra)}
    payload = sorted(keys & PAYLOAD_KEYS)
    if payload:
        raise PayloadCaptureError(
            f"{owner} carries payload-shaped field(s) {payload}; record a "
            "sha256 digest or report reference instead — prompts, transcripts, "
            "exploit payloads, weights and activations never enter the capsule "
            "(ADR-0167 I-5)"
        )
    c4 = sorted(keys & C4_INLINE_KEYS)
    if c4:
        raise GuardrailDuplicationError(
            f"{owner} re-records C4 guardrail-decision field(s) {c4}; reference "
            "the facets.safety decision by digest via guardrail_decision_ref "
            "instead (ADR-0167 D6, spec §3.15 — no NF-131..140 duplication)"
        )
