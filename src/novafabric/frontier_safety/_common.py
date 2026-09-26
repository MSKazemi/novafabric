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
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}\Z")

#: An external artifact may also be named by locator — an https URL, an
#: evaluator's report URI. A URI *shape* only; nothing here dereferences it
#: (offline by default, and I-2's fail-open rule forbids a network call on the
#: capture path).
_URI_RE = re.compile(r"^[a-z][a-z0-9+.\-]*://\S+\Z")

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

#: Known payload key spellings. Kept (and exported) as the documented examples
#: of what I-5 forbids; the guard itself matches the broader
#: :data:`PAYLOAD_KEY_MARKERS` on *normalised* keys, so every name here — and
#: every camelCase / kebab-case / suffixed variant of it — is rejected.
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

#: Substring markers matched against every *normalised* extra key (lower-cased,
#: non-alphanumerics stripped — see :func:`normalise_key`). An exact-match
#: denylist is trivially evaded (``exploitSteps``, ``exploit-steps``,
#: ``ExfiltratedData``); a substring match on the normalised form is not.
#: Same approach as ``novafabric.hitl._records._PAYLOAD_KEY_MARKERS``, with a
#: marker set tuned to this cluster: markers that would reject legitimate P2
#: control-decision material (``command``, ``step``, ``shell``) are kept local
#: to :class:`~novafabric.frontier_safety.alignment.AutonomyAttempt`.
PAYLOAD_KEY_MARKERS: tuple[str, ...] = (
    "prompt",
    "transcript",
    "payload",
    "exploit",
    "weights",
    "activation",
    "completion",
    "responsetext",
    "rawoutput",
    "rawinput",
    "stdout",
    "stderr",
    "exfil",
)

#: A key whose normalised form ends with one of these *and* whose value is a
#: ``sha256:`` digest is a reference to the artifact, not the artifact —
#: ``red_team_transcript_digest`` is exactly the I-5-compliant way to point at a
#: transcript. Only a strict digest qualifies: a URI can carry arbitrary text
#: in its path, so a URI-valued ``prompt_ref`` is still rejected.
REFERENCE_KEY_SUFFIXES: tuple[str, ...] = ("ref", "digest", "sha256", "hash")

#: Field names that only a C4 guardrail-decision object (ADR-0145,
#: ``novafabric.safety.decisions.GuardrailDecision``) carries. Their presence
#: on a frontier-safety object means the guardrail decision is being
#: re-recorded rather than referenced (spec §3.15). Matched on normalised keys
#: (exact, not substring: ``guardrail_decision_ref`` is the sanctioned link).
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

#: Upper bound on any free-form string value a P3 object carries (extra fields,
#: at any nesting depth, plus ``schema_version`` and a string ``verdict``).
#: Declared reference fields have their own :data:`MAX_REF_LENGTH` bound. A
#: label or identifier fits comfortably; a transcript or script does not.
MAX_FREE_STRING_LENGTH = 512

_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def normalise_key(key: str) -> str:
    """Return ``key`` lower-cased with every non-alphanumeric character removed.

    ``exploitSteps``, ``exploit-steps`` and ``Exploit_Steps`` all normalise to
    ``exploitsteps``, so one marker catches every spelling.
    """
    return _NON_ALNUM_RE.sub("", key.lower())


def _iter_items(value: object, *, depth: int = 0) -> list[tuple[str, object]]:
    """Return every ``(key, value)`` pair nested inside ``value``, depth-bounded.

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
    items: list[tuple[str, object]] = []
    if isinstance(value, dict):
        for key, item in value.items():
            items.append((str(key), item))
            items.extend(_iter_items(item, depth=depth + 1))
    elif isinstance(value, (list, tuple)):
        for item in value:
            items.extend(_iter_items(item, depth=depth + 1))
    return items


def _iter_keys(value: object, *, depth: int = 0) -> list[str]:
    """Return every mapping key nested inside ``value``, bounded by depth."""
    return [key for key, _ in _iter_items(value, depth=depth)]


def _is_digest_reference(key: str, value: object) -> bool:
    """True when ``key`` names a reference and ``value`` is a strict digest."""
    return (
        normalise_key(key).endswith(REFERENCE_KEY_SUFFIXES)
        and isinstance(value, str)
        and _DIGEST_RE.match(value) is not None
    )


def find_marker_keys(extra: dict[str, Any] | None, markers: tuple[str, ...]) -> list[str]:
    """Return the (original-spelling) extra keys that contain any ``markers``.

    Keys are normalised with :func:`normalise_key` before the substring test,
    and the walk descends into nested mappings and lists (bounded by
    :data:`MAX_EXTRA_DEPTH`). A digest-valued reference key
    (:data:`REFERENCE_KEY_SUFFIXES`) is exempt: it points at the artifact
    rather than holding it. Sorted and de-duplicated for a deterministic
    error message.
    """
    if not extra:
        return []
    hits = {
        key
        for key, value in _iter_items(extra)
        if any(marker in normalise_key(key) for marker in markers)
        and not _is_digest_reference(key, value)
    }
    return sorted(hits)


def _iter_strings(value: object, *, depth: int = 0) -> list[str]:
    """Return every string (keys included) nested inside ``value``, bounded."""
    if depth > MAX_EXTRA_DEPTH:
        raise PayloadCaptureError(
            f"extra fields nest deeper than {MAX_EXTRA_DEPTH} levels; a "
            "frontier-safety record holds references, not documents (I-5)"
        )
    if isinstance(value, str):
        return [value]
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            found.append(str(key))
            found.extend(_iter_strings(item, depth=depth + 1))
    elif isinstance(value, (list, tuple)):
        for item in value:
            found.extend(_iter_strings(item, depth=depth + 1))
    return found


def check_string_lengths(
    values: dict[str, Any],
    *,
    owner: str,
    limit: int = MAX_FREE_STRING_LENGTH,
) -> None:
    """Reject any string in ``values`` (at any bounded depth) over ``limit``.

    Raises:
        PayloadCaptureError: naming the top-level field only — never the value,
            which is exactly the text this check exists to keep out of logs.
    """
    for field, value in values.items():
        if any(len(s) > limit for s in _iter_strings({field: value})):
            raise PayloadCaptureError(
                f"{owner}.{field} holds a string over the {limit}-char limit; "
                "free-form values are labels, not documents — record a sha256 "
                "digest of the content instead (ADR-0167 I-5)"
            )


def check_extra_fields(extra: dict[str, Any] | None, *, owner: str) -> None:
    """Reject payload-shaped or C4-duplicating keys in an object's extra fields.

    Keys are normalised (:func:`normalise_key`) and matched against
    :data:`PAYLOAD_KEY_MARKERS` by substring, so case, separators and
    prefixes/suffixes do not evade the guard.

    Args:
        extra: the object's ``model_extra`` (undeclared fields kept by
            ``extra="allow"``). ``None`` or empty passes.
        owner: the object's class name, for the error message.

    Raises:
        PayloadCaptureError: a key names a prompt, transcript, payload, weights,
            activations, process output or exfiltrated data (I-5, ADR-0009,
            ADR-0021 §4).
        GuardrailDuplicationError: a key belongs to a C4 guardrail-decision
            object; reference it via ``guardrail_decision_ref`` instead.
    """
    if not extra:
        return
    payload = find_marker_keys(extra, PAYLOAD_KEY_MARKERS)
    if payload:
        raise PayloadCaptureError(
            f"{owner} carries payload-shaped field(s) {payload}; record a "
            "sha256 digest or report reference instead — prompts, transcripts, "
            "exploit payloads, weights and activations never enter the capsule "
            "(ADR-0167 I-5)"
        )
    c4_normalised = {normalise_key(k) for k in C4_INLINE_KEYS}
    c4 = sorted({k for k in _iter_keys(extra) if normalise_key(k) in c4_normalised})
    if c4:
        raise GuardrailDuplicationError(
            f"{owner} re-records C4 guardrail-decision field(s) {c4}; reference "
            "the facets.safety decision by digest via guardrail_decision_ref "
            "instead (ADR-0167 D6, spec §3.15 — no NF-131..140 duplication)"
        )
