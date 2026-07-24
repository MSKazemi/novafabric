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

"""Frontier-safety facet — ADR-0167 D1/P1 (NF-351, NF-353).

Records that a named frontier-safety-framework threshold evaluation (RSP/ASL,
Preparedness, FSF-CCL) *ran* on a run, and which published framework
commitment a run or an observed incident implicates — by reference, into the
optional ``facets.frontier_safety`` block.

Five invariants from ADR-0167 / the NF-351-360 spec §3 shape every choice here:

- **I-1 Record-only, never enforcing.** NovaFabric records that an external
  evaluator, protocol, or human decided something. Nothing here blocks,
  gates, rewrites, quarantines or refuses a workload, and this module exposes
  no entry point that could be mistaken for one.
- **I-2 Additive-first, fail-open.** The facet lives in optional
  ``facets.frontier_safety``. Absent safety material means *no facet* — never
  an empty one, never an exception. A capsule with nothing to say here is
  byte-identical to one captured before this feature existed. Safety evidence
  must never block the very workload it observes.
- **I-3 Never a computed verdict.** NovaFabric must never author a
  frontier-safety verdict. ``verdict`` is ``null``, or it is accompanied by
  the external ``verdict_ref`` + ``verdict_source`` that says who issued it.
  See :class:`ComputedVerdictError` — this is enforced, not merely documented.
- **I-4 Absent is not false.** A missing verdict means *not evaluated*. It
  never means safe and it never means unsafe. No boolean anywhere in this
  module collapses that third state into two.
- **I-5 No payloads.** References, digests, versions and identifiers only.
  Eval results, commitment texts, prompts, weights and red-team transcripts
  never enter the capsule through this path (ADR-0009, ADR-0021 §4).

P1 is the facet, the NF-351 threshold-eval binding and the NF-353 commitment
binding. The control-protocol decision (NF-352), tripwire (NF-357) and the
alignment-risk objects (NF-354/355/356/358) are P2/P3 and deliberately absent
— the facet's ``extra="allow"`` config is what lets a later slice add them
without a schema break.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

FACET_NAME = "frontier_safety"
SCHEMA_VERSION = "0.1.0"

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

#: What a commitment binding is about (NF-353). `incident` is the
#: incident→commitment direction the spec requires; the others cover the
#: run→commitment direction.
SubjectType = Literal["run", "threshold_eval", "incident", "other"]

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
        raise InvalidReferenceError(
            f"{field} must be 'sha256:<64 hex>' or a URI, got {value!r}"
        )
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
            f"{field} must be a content digest 'sha256:<64 hex>', not a locator; "
            f"got {ref!r}"
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


# ── Objects ───────────────────────────────────────────────────────────────


class ThresholdEval(_ExternalVerdict):
    """A dangerous-capability threshold evaluation that ran (NF-351).

    Records *that* a named RSP/ASL, Preparedness or FSF-CCL threshold eval
    executed, bound to the NF-154 eval-integrity record that proves it, with
    the external evaluator's verdict by reference. It does not record whether
    the threshold was met — that determination belongs to the framework's
    evaluator (ADR-0167 D1, I-3).
    """

    model_config = ConfigDict(extra="allow")

    schema_version: str = SCHEMA_VERSION
    framework: Framework
    framework_version: str
    #: e.g. `ASL-3`, `preparedness.cyber.high`, `fsf.ccl.cbrn`.
    threshold_id: str
    #: Literal True, not bool. The object's whole meaning is "this eval ran";
    #: `eval_ran: false` would be an object asserting the absence of an
    #: evaluation, which under I-2 is recorded by there being no facet at all,
    #: not by a facet claiming a negative.
    eval_ran: Literal[True] = True
    #: Digest of the NF-154 eval-integrity record. A digest, not a URI: this
    #: is the binding that makes "the eval ran" checkable.
    eval_ref: str
    #: Digest of the sealed capsule root this facet is bound into. Optional in
    #: P1: the root is only known once the capsule is sealed, and a facet
    #: built during a run legitimately does not have it yet.
    bound_root: str | None = None

    @field_validator("eval_ref", mode="before")
    @classmethod
    def _check_eval_ref(cls, v: object) -> str:
        return _validate_digest(v, field="eval_ref")

    @field_validator("bound_root", mode="before")
    @classmethod
    def _check_bound_root(cls, v: object) -> str | None:
        if v is None:
            return None
        return _validate_digest(v, field="bound_root")

    @field_validator("framework_version", "threshold_id")
    @classmethod
    def _check_non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError(
                "framework_version and threshold_id must be non-empty; an "
                "unversioned threshold cannot be reconciled against the "
                "published framework text later (NF-351)"
            )
        return v


class CommitmentBinding(_ExternalVerdict):
    """Which published framework commitment a run or incident implicates (NF-353).

    Maps a subject — the run, a threshold eval, or an observed incident — to a
    specific published commitment, by a digest of the commitment text. It
    records *which* commitment is implicated; it never records whether the
    commitment was *satisfied*. There is deliberately no ``satisfied`` field:
    that judgement is the framework owner's, and a boolean here would be
    NovaFabric adjudicating compliance (ADR-0167 D1, I-3).
    """

    model_config = ConfigDict(extra="allow")

    schema_version: str = SCHEMA_VERSION
    framework: Framework
    commitment_id: str
    #: Digest of the published commitment text/section, never the text itself
    #: (I-5). A digest rather than a URI so the commitment a run was held to
    #: cannot be edited out from under the sealed capsule.
    commitment_digest: str
    #: What implicates the commitment: the run, an object, or an incident.
    subject_type: SubjectType = "run"
    #: Digest/URI of that subject. Optional: a run-level binding whose subject
    #: is the capsule itself is already identified by the capsule it lives in,
    #: and requiring a self-reference would be ceremony, not evidence.
    implicated_by_ref: str | None = None

    @field_validator("commitment_digest", mode="before")
    @classmethod
    def _check_commitment_digest(cls, v: object) -> str:
        return _validate_digest(v, field="commitment_digest")

    @field_validator("implicated_by_ref", mode="before")
    @classmethod
    def _check_implicated_by_ref(cls, v: object) -> str | None:
        if v is None:
            return None
        return _validate_ref(v, field="implicated_by_ref")

    @field_validator("commitment_id")
    @classmethod
    def _check_commitment_id(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("commitment_id must be non-empty (NF-353)")
        return v


class VerificationFlags(BaseModel):
    """What a verifier actually checked.

    Every flag defaults to ``None``, meaning *not checked* — distinct from
    ``False``, meaning *checked and failed*. P1 performs no seal verification,
    so ``sealed_into_root`` stays ``None``; defaulting it to ``True`` would
    launder an unperformed check into a safety record, and defaulting it to
    ``False`` would slander a seal nobody looked at.
    """

    model_config = ConfigDict(extra="allow")

    eval_ref_resolvable: bool | None = None
    verdict_by_reference: bool | None = None
    sealed_into_root: bool | None = None


class FrontierSafetyFacet(BaseModel):
    """The optional ``facets.frontier_safety`` block (NF-351, I-2).

    Singular ``threshold_eval`` / ``commitment_binding`` members, matching the
    spec §4.1/§4.3 wire shape exactly. A plural form would be more general,
    but forking from the published shape for generality we do not yet need
    would cost every downstream consumer; ``extra="allow"`` lets a later slice
    add a plural sibling additively if a run ever implicates several.
    """

    model_config = ConfigDict(extra="allow")

    schema_version: str = SCHEMA_VERSION
    threshold_eval: ThresholdEval | None = None
    commitment_binding: CommitmentBinding | None = None
    verified: VerificationFlags | None = None

    @property
    def has_material(self) -> bool:
        """True when the facet carries safety evidence worth sealing.

        Verification flags alone do not count: a facet holding only "we
        checked nothing" adds a block, a schema version and a seal surface
        while answering none of the questions ADR-0167 exists to answer.
        """
        return self.threshold_eval is not None or self.commitment_binding is not None


# ── Construction ──────────────────────────────────────────────────────────


def build_facet(
    *,
    threshold_eval: ThresholdEval | None = None,
    commitment_binding: CommitmentBinding | None = None,
    verified: VerificationFlags | None = None,
) -> FrontierSafetyFacet:
    """Assemble the frontier-safety facet from external safety references.

    Keyword-only: every member is optional and the two objects are easy to
    transpose positionally, which would silently bind a run to the wrong
    commitment.
    """
    return FrontierSafetyFacet(
        threshold_eval=threshold_eval,
        commitment_binding=commitment_binding,
        verified=verified,
    )


def attach_facet(
    capsule: dict[str, Any], facet: FrontierSafetyFacet
) -> dict[str, Any]:
    """Attach the frontier-safety facet to a capsule dict, additively.

    Writes nothing when the facet carries no safety material: a run with no
    frontier-safety evidence must be byte-identical to one captured before
    this feature existed (I-2, fail-open). Returns a new dict; the input is
    not mutated.
    """
    if not facet.has_material:
        return capsule
    out = dict(capsule)
    facets = dict(out.get("facets") or {})
    # exclude_none so an unchecked verification flag is *absent*, not `null`,
    # and so `verdict: null` does not have to be re-litigated by every reader:
    # absence and null both mean "not evaluated" (I-4), and the ADR's
    # verdict-by-reference contract is carried by `verdict_ref`.
    facets[FACET_NAME] = facet.model_dump(exclude_none=True)
    out["facets"] = facets
    return out


def facet_from_capsule(capsule: dict[str, Any]) -> FrontierSafetyFacet | None:
    """Read the frontier-safety facet back out of a capsule dict.

    Returns None when the capsule has no facet — the overwhelmingly common
    case, and not an error (I-2 fail-open).
    """
    facets = capsule.get("facets")
    if not isinstance(facets, dict):
        return None
    block = facets.get(FACET_NAME)
    if not isinstance(block, dict):
        return None
    return FrontierSafetyFacet.model_validate(block)


# ── Verification ──────────────────────────────────────────────────────────


def verify_eval_binding(threshold_eval: ThresholdEval, eval_record: str | bytes) -> bool:
    """Re-verify a threshold eval's ``eval_ref`` against the NF-154 record.

    Offline and content-only: this checks that the capsule names the eval
    record the caller holds. It says nothing about what the eval concluded —
    that stays behind ``verdict_ref`` with its external source.
    """
    return threshold_eval.eval_ref == digest_ref(eval_record)


def verify_commitment_binding(
    binding: CommitmentBinding, commitment_text: str | bytes
) -> bool:
    """Re-verify a commitment binding against the published commitment text.

    The point of the digest: an auditor can prove which version of the
    published framework text the run was bound to, even after the lab
    republishes it.
    """
    return binding.commitment_digest == digest_ref(commitment_text)
