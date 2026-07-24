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

"""Risk-transfer actuarial facet + incident-loss bind — ADR-0170 P1 (NF-381/382).

Renders what a sealed capsule already contains as the two things the
risk-transfer layer asks for first: *loss-relevant features* an actuary can
consume (counts and digests, never trace content), and an *incident-loss
record* bound by digest to the NF-233 DFIR bundle that reconstructed the
incident.

Four invariants from ADR-0170 shape every choice in this module:

- **I-1 Record-only.** NovaFabric records evidence for the risk-transfer
  layer. It does not underwrite, price, bind, or rate a policy; it does not
  adjudicate a claim or pay out; it does not assign legal liability. Nothing
  here computes a premium, a risk score, or a loss total — every one of those
  is the insurer's, adjuster's, actuary's, or court's function. The evidence
  *supports* their determination; it never *is* that determination.
- **I-2 Digests and counts only.** No incident narrative, no claimant or
  victim data, no raw DFIR content, no trace text enters the facet. A
  failure-mode signature is the digest of an error *type*; the message that
  would carry the story is never read.
- **I-3 Additive-first / fail-open.** The facet lives in optional
  ``facets.risk_transfer``. A run with no risk-transfer material produces no
  facet, and a capsule without one is byte-identical to one captured before
  this feature existed.
- **I-4 Declared is not measured.** Every loss figure carries ``declared_by``
  and ``source``. A figure someone asserted must stay visibly distinct from
  one NovaFabric observed — laundering the first into the second is exactly
  the failure this facet exists to prevent.

**Absent is not false.** No loss record on a capsule means *not recorded*,
never *no loss occurred*; no ``guardrail_trip`` feature means the guardrail
evidence was not present to count, not that nothing tripped. That is why
:func:`extract_loss_features` emits a feature only when the surface it counts
is actually present, and emits ``count=0`` — a recorded zero — when it is
present and empty. The difference between a clean run and an unreported one
is the whole value of the record to an underwriter.

**Unbound is a finding, not an error.** An ``incident_bundle_ref`` that does
not resolve is recorded as ``unbound: true`` (ADR-0170 D2): never dropped,
never silently treated as bound, never a hard failure. An incident loss whose
supporting bundle cannot be produced is precisely what an underwriter needs
to see.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Iterable, Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# The settlement facet's payment-secret scanner, reused as a *shared control*
# rather than reimplemented. The facet shapes below are deliberately ADR-0170's
# own (see `Money`), but a second, independently-drifting PAN/IBAN/credential
# detector would be worse than the coupling: two detectors disagree, and the
# one that is wrong is the one nobody is looking at. Note the error it raises
# keeps its settlement name — an odd read in an insurance module, kept because
# renaming a security control's exception per call site hides which rule fired.
from novafabric.settlement.facet import (
    PaymentSecretRejectedError,
    reject_payment_secrets,
)

FACET_NAME = "risk_transfer"
SCHEMA_VERSION = "0.1.0"

#: Loss-feature kinds an actuary prices from an execution trace (D1).
LossFeatureKind = Literal[
    "deviation",
    "failure_mode",
    "error_propagation",
    "tool_error_rate",
    "escalation",
    "override",
    "guardrail_trip",
]

#: Where a loss figure came from (D2).
#:
#: `observed` is the only value meaning NovaFabric or an instrument measured
#: the figure. `declared` and `estimated_by_third_party` are both *assertions*
#: — they differ only in who made them, not in their evidential weight.
LossSource = Literal["declared", "observed", "estimated_by_third_party"]

#: A reference must be a `sha256:<64 hex>` digest.
#:
#: The spec's wider "reference (URI) **or** digest" shape is deferred: P1's
#: job is the *binding*, and only a digest binds. A URI names a place a DFIR
#: bundle was, which an offline verifier cannot check and which says nothing
#: about the bytes — accepting one would let an incident-loss record claim a
#: binding it does not have, and `unbound` would then never fire.
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class InvalidReferenceError(Exception):
    """Raised when a reference is not a ``sha256:`` digest.

    Deliberately **not** a ``ValueError``. Pydantic v2 catches ``ValueError``
    inside a validator and folds it into a ``ValidationError`` alongside
    ordinary shape complaints, destroying the named type — and the most likely
    bad reference here is an inlined DFIR excerpt or a claim number, which the
    caller must be told about specifically.
    """


class FloatAmountRejectedError(Exception):
    """Raised when a money amount is offered as a float (or a string).

    See :class:`Money` for why. Not a ``ValueError``, for the reason given on
    :class:`InvalidReferenceError`.
    """


class UnquantifiedFeatureError(Exception):
    """Raised when a loss feature carries neither a count nor a value digest.

    A feature naming a signal with nothing measured is not an actuarial input;
    it is an assertion that something happened, which is the shape this module
    exists to keep out.
    """


class MissingDeclaredByError(Exception):
    """Raised when a loss item does not name who declared it (I-4).

    A figure with no ``declared_by`` cannot be told apart from one NovaFabric
    measured, which is the one confusion ADR-0170 forbids.
    """


class MissingIncidentBundleError(Exception):
    """Raised when an incident-loss record is built with no bundle reference.

    ADR-0170 D2: this object MUST NOT be emitted without a bound NF-233
    bundle. Note the asymmetry with ``unbound``: a ref that *fails to resolve*
    is recorded (fail-open, I-3), because that is a fact about the evidence. A
    caller that passes no ref at all has a bug — there is nothing to record.
    """


# ── References ────────────────────────────────────────────────────────────


def digest_artifact(content: str | bytes) -> str:
    """Return the ``sha256:`` digest of a DFIR bundle or other artifact.

    Callers hash the artifact and keep the artifact out of the capsule. The
    form matches every other digest in the capsule, so a verifier does not
    have to know which subsystem wrote it.
    """
    raw = content.encode("utf-8") if isinstance(content, str) else content
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


def verify_ref_binding(ref: str | None, artifact: str | bytes) -> bool:
    """Re-verify a reference against the artifact it claims to bind.

    Returns False for a missing reference. An unbound record is not
    "trivially valid" — it is the case a verifier exists to surface, and
    returning True would pass exactly the records with nothing to check.
    """
    if not ref:
        return False
    return ref == digest_artifact(artifact)


def _validate_ref(value: str | None) -> str | None:
    if value is None:
        return None
    if not _DIGEST_RE.match(value):
        raise InvalidReferenceError(
            f"reference {value!r} is not a 'sha256:<64 hex>' digest; DFIR "
            "content, narrative text and URIs are never stored here "
            "(ADR-0170 D2, I-2)"
        )
    return value


# ── Models ────────────────────────────────────────────────────────────────


class Money(BaseModel):
    """A loss figure, as integer minor units plus an ISO-4217 code.

    Never a float. A loss schedule is added up by an adjuster, and binary
    floating point cannot represent 420.00 exactly — a schedule of float items
    does not sum to the same number twice, and an actuarial input that does
    not sum exactly is not an actuarial input. Minor units keep every item and
    every downstream total exact and integral.

    The currency code is what makes minor units meaningful: 42000 is USD
    420.00 but JPY 42000 (zero-decimal) and KWD 42.000 (three-decimal).
    Storing the amount without the code would be storing a number, not a sum
    of money — and an insurer reading a cross-border loss schedule is the
    consumer least able to guess.

    Defined here rather than imported from ``settlement.facet``: the two
    amounts are not the same kind of fact. A settlement amount is an *observed
    network confirmation*; a loss amount is almost always a *declared* figure
    (see :class:`LossItem`). ADR-0163 owns the first shape and ADR-0170 the
    second, and sharing the model would let a change to one ADR silently
    alter the other's evidence.
    """

    model_config = ConfigDict(extra="allow")

    #: Minor units (cents, satang, …). Non-negative: a recovery or a subrogated
    #: credit is its own positively-signed item under its own category, not a
    #: negative loss, so a negative here means a caller bug — and a negative
    #: item hiding inside a schedule is the easiest way to understate a loss.
    amount_minor: int = Field(ge=0)
    #: ISO-4217 alpha-3. Shape-validated only — pinning the full code list
    #: would mean shipping a table that goes stale on every redenomination, to
    #: reject a value NovaFabric only ever records (I-1).
    currency: str

    @field_validator("amount_minor", mode="before")
    @classmethod
    def _reject_non_integer(cls, value: Any) -> Any:
        # `mode="before"` on purpose: pydantic's lax coercion would silently
        # accept 42000.0 (and "42000"), so a caller that thinks in floats
        # would get a facet that looks correct right up to the first amount
        # with a fractional part. Refusing the *type* tells them now.
        if isinstance(value, bool) or not isinstance(value, int):
            raise FloatAmountRejectedError(
                f"amount_minor must be an integer number of minor units, got "
                f"{type(value).__name__} {value!r}; money is never a float in "
                "an actuarial record (ADR-0170 D2)"
            )
        return value

    @field_validator("currency")
    @classmethod
    def _validate_currency(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Z]{3}", value):
            raise ValueError(
                f"currency {value!r} is not an ISO-4217 alpha-3 code "
                "(three uppercase letters, e.g. 'USD')"
            )
        return value


class LossFeature(BaseModel):
    """One loss-relevant feature extracted from a sealed capsule (NF-381).

    A feature is a **count** or a **digest**, never content: the count of
    guardrail trips, the digest of a failure-mode signature. What tripped the
    guardrail and what the failure said stay in the capsule they came from
    (I-2).
    """

    model_config = ConfigDict(extra="allow")

    feature: str
    kind: LossFeatureKind
    #: Occurrences observed in ``window``. ``0`` is a *recorded zero* and is
    #: meaningfully different from the feature being absent (see the module
    #: docstring).
    count: int | None = Field(default=None, ge=0)
    #: Denominator for a rate-shaped feature, recorded alongside ``count``
    #: rather than divided into it. NovaFabric records the two exact integers;
    #: computing the rate is the consumer's arithmetic — and a stored float
    #: rate would be both lossy and one step towards the rating this module
    #: does not do (I-1).
    denominator: int | None = Field(default=None, ge=0)
    #: Digest of a signature (e.g. an error kind) when the feature is not a
    #: count. Opaque by design: two runs that failed the same way get equal
    #: digests, which is what clustering failure modes needs, without the
    #: facet ever naming the failure.
    value_digest: str | None = None
    #: Scope the feature was measured over. Free-form (``"run"``, a step id, a
    #: window label) because ADR-0170 does not enumerate it and inventing a
    #: closed set here would reject windows the ADR never ruled out.
    window: str = "run"

    @field_validator("value_digest")
    @classmethod
    def _check_digest(cls, value: str | None) -> str | None:
        return _validate_ref(value)

    @model_validator(mode="after")
    def _require_quantification(self) -> LossFeature:
        if self.count is None and self.value_digest is None:
            raise UnquantifiedFeatureError(
                f"loss feature {self.feature!r} carries neither a count nor a "
                "value_digest; a feature with nothing measured is an "
                "assertion, not an actuarial input (ADR-0170 D1)"
            )
        return self


class ActuarialBlock(BaseModel):
    """``risk_transfer.actuarial`` — loss features, not a rating (D1)."""

    model_config = ConfigDict(extra="allow")

    schema_version: str = SCHEMA_VERSION
    loss_features: list[LossFeature] = Field(default_factory=list)
    #: Digests of the capsule(s) the features were extracted from.
    capsule_refs: list[str] = Field(default_factory=list)
    #: Digest of the capsule root this block is sealed into.
    bound_root: str | None = None

    @field_validator("capsule_refs")
    @classmethod
    def _check_capsule_refs(cls, value: list[str]) -> list[str]:
        for ref in value:
            _validate_ref(ref)
        return value

    @field_validator("bound_root")
    @classmethod
    def _check_bound_root(cls, value: str | None) -> str | None:
        return _validate_ref(value)


class LossItem(BaseModel):
    """One declared or observed loss figure, with its provenance (D2).

    ``declared_by`` and ``source`` are mandatory and never omitted from the
    dump: they are what keeps a figure someone asserted distinguishable from
    one that was measured (I-4). NovaFabric records who claimed what; it does
    not adjudicate the claim, total the schedule, or price anything.
    """

    model_config = ConfigDict(extra="allow")

    #: Loss category (``remediation``, ``downtime``, …). Free-form: insurers'
    #: loss taxonomies differ per line of business, and a closed set here
    #: would force a real declared loss into the wrong bucket — a worse
    #: outcome than an unfamiliar label the consumer can map.
    category: str
    amount: Money
    #: What the figure rests on — a digest, an invoice reference, a named
    #: calculation. Optional, because a declared figure with no basis is a
    #: real (and revealing) state of the evidence.
    basis: str | None = None
    #: The party that declared or observed the figure. Required (I-4).
    declared_by: str
    source: LossSource

    @field_validator("declared_by")
    @classmethod
    def _require_declarant(cls, value: str) -> str:
        if not value.strip():
            raise MissingDeclaredByError(
                "loss item has no declared_by; a figure with no named "
                "declarant cannot be told apart from a measured one "
                "(ADR-0170 D2, I-4)"
            )
        return value


def is_measured(item: LossItem) -> bool:
    """Return True only for a figure an instrument or NovaFabric observed.

    ``declared`` and ``estimated_by_third_party`` both return False: both are
    assertions, differing only in who made them. This reads the recorded
    provenance back out — it does not judge the figure.
    """
    return item.source == "observed"


class IncidentLoss(BaseModel):
    """``risk_transfer.incident_loss`` — declared losses bound to a DFIR bundle (D2).

    The NF-233 bundle is bound **by digest and never re-reconstructed**: E4
    (ADR-0155) owns incident reconstruction and custody, and this record only
    layers risk-transfer meaning on top of it.
    """

    model_config = ConfigDict(extra="allow")

    schema_version: str = SCHEMA_VERSION
    #: Digest of the NF-233 DFIR bundle. Required — D2 forbids emitting this
    #: object without one.
    incident_bundle_ref: str
    #: True when ``incident_bundle_ref`` could not be resolved to the bytes it
    #: names. Recorded, never fatal, never dropped (see the module docstring).
    unbound: bool = False
    loss_items: list[LossItem] = Field(default_factory=list)
    quantified_at: str | None = None
    bound_root: str | None = None

    @field_validator("incident_bundle_ref", "bound_root")
    @classmethod
    def _check_refs(cls, value: str | None) -> str | None:
        return _validate_ref(value)


class RiskTransferFacet(BaseModel):
    """The optional ``facets.risk_transfer`` block (I-3).

    P1 carries two of ADR-0170's objects. The liability chain (NF-383),
    coverage triggers (NF-386) and the rest arrive in later phases and are
    deliberately absent here rather than stubbed: an empty object in the
    sealed root would read as "recorded, nothing found".
    """

    model_config = ConfigDict(extra="allow")

    schema_version: str = SCHEMA_VERSION
    actuarial: ActuarialBlock | None = None
    incident_loss: IncidentLoss | None = None

    @model_validator(mode="after")
    def _reject_secrets(self) -> RiskTransferFacet:
        """Enforce I-2 on the whole facet, including ``extra`` fields.

        Run as a model validator rather than inside :func:`build_facet` so
        that no construction path — direct instantiation, ``model_validate``
        of untrusted JSON, ``model_copy(update=…)`` — can produce a facet
        carrying an account number or credential. ``extra="allow"`` is
        mandated for these models, so the structural "digests and counts only"
        discipline needs this backstop on the open part of the shape.

        Raises:
            PaymentSecretRejectedError: naming the field and the rule, never
                the value.
        """
        reject_payment_secrets(self.model_dump())
        return self


# ── Loss-feature extraction (NF-381) ──────────────────────────────────────


def extract_loss_features(capsule: Mapping[str, Any]) -> list[LossFeature]:
    """Extract loss-relevant features from an already-sealed capsule.

    Counts and digests only (I-2). Reads exclusively surfaces that
    unambiguously evidence a loss-relevant signal:

    - ``facets.safety`` → a ``guardrail_trip`` count (blocked and rewritten
      decisions), *only when that facet is present*. No safety facet means the
      evidence was not there to count, not that nothing tripped.
    - ``status`` / ``error`` → a ``failure_mode`` signature, as the digest of
      ``error.type`` (an exception class name). ``error.message`` is never
      read: it is sanitized for secrets but it is still the narrative, and
      narrative is what I-2 keeps out of the facet.

    Deliberately does *not* derive a tool-error rate: the capsule root carries
    ``tool_call_count`` but no error count (per-call outcomes live in the
    ``tool_calls_ref`` sidecar), and guessing a numerator from
    ``mutating_tool_count`` or an exit code would manufacture an actuarial
    input nobody measured. A caller that has the sidecar can pass the feature
    in explicitly.

    Fail-open (I-3): a capsule with no loss-relevant surface yields ``[]``,
    never an exception.
    """
    features: list[LossFeature] = []

    facets = capsule.get("facets")
    safety = facets.get("safety") if isinstance(facets, Mapping) else None
    if isinstance(safety, Mapping):
        decisions = safety.get("decisions")
        trips = 0
        if isinstance(decisions, Iterable) and not isinstance(decisions, (str, bytes)):
            trips = sum(
                1
                for d in decisions
                if isinstance(d, Mapping) and d.get("disposition") in ("block", "rewrite")
            )
        features.append(
            LossFeature(feature="guardrail_trip", kind="guardrail_trip", count=trips)
        )

    error = capsule.get("error")
    error_type = error.get("type") if isinstance(error, Mapping) else None
    if capsule.get("status") in ("failure", "partial", "aborted") and isinstance(
        error_type, str
    ):
        features.append(
            LossFeature(
                feature="run_failure",
                kind="failure_mode",
                count=1,
                value_digest=digest_artifact(error_type),
            )
        )

    return features


# ── Assembly ──────────────────────────────────────────────────────────────


def build_actuarial(
    *,
    loss_features: Iterable[LossFeature] = (),
    capsule_refs: Iterable[str] = (),
    bound_root: str | None = None,
) -> ActuarialBlock | None:
    """Build the actuarial block, or ``None`` when there is nothing to record.

    Fail-open (I-3): a run that exhibited no loss-relevant feature yields
    ``None``. A block naming capsules but carrying no feature is not evidence
    — it asserts that features were looked for, which is a claim about a
    collection process this module cannot make on the caller's behalf.
    """
    features = list(loss_features)
    if not features:
        return None
    return ActuarialBlock(
        loss_features=features,
        capsule_refs=list(capsule_refs),
        bound_root=bound_root,
    )


def build_incident_loss(
    *,
    incident_bundle_ref: str | None,
    loss_items: Iterable[LossItem] = (),
    resolver: Callable[[str], str | bytes | None] | None = None,
    quantified_at: str | None = None,
    bound_root: str | None = None,
) -> IncidentLoss:
    """Bind declared loss items to an NF-233 DFIR bundle by digest (D2).

    ``resolver`` is asked to produce the bytes the digest names. The record is
    marked ``unbound`` when the resolver returns ``None`` (the bundle cannot be
    produced) **or** returns bytes whose digest differs (the ref names
    something else). Both are the same fact for an underwriter: the loss has
    no verifiable incident behind it. Calling the second case "bound" because
    a lookup succeeded would be the more dangerous error of the two.

    With no ``resolver`` the record is left ``unbound=False``: the caller has
    asserted a binding this function was given no way to check, and inventing
    an ``unbound`` mark for an unchecked ref would report a finding nobody
    made.

    Raises:
        MissingIncidentBundleError: if ``incident_bundle_ref`` is absent. See
            the exception for why this is not the fail-open case.
        InvalidReferenceError: if the ref is not a ``sha256:`` digest.
    """
    if not incident_bundle_ref:
        raise MissingIncidentBundleError(
            "an incident-loss record requires a bound NF-233 DFIR bundle "
            "digest; NovaFabric does not reconstruct the incident (ADR-0170 "
            "D2 — E4/ADR-0155 owns forensics)"
        )
    unbound = False
    if resolver is not None:
        try:
            artifact = resolver(incident_bundle_ref)
        except Exception:  # noqa: BLE001 - see below
            # A resolver that raised told us nothing about the bundle, only
            # about itself. Fail-open (I-3): record the ref as unresolved
            # rather than failing the capsule over a lookup error.
            artifact = None
        unbound = artifact is None or not verify_ref_binding(
            incident_bundle_ref, artifact
        )
    return IncidentLoss(
        incident_bundle_ref=incident_bundle_ref,
        unbound=unbound,
        loss_items=list(loss_items),
        quantified_at=quantified_at,
        bound_root=bound_root,
    )


def build_facet(
    *,
    actuarial: ActuarialBlock | None = None,
    incident_loss: IncidentLoss | None = None,
) -> RiskTransferFacet | None:
    """Build the risk-transfer facet, or ``None`` when there is nothing to record.

    Fail-open (I-3): a run with neither loss features nor an incident loss
    yields ``None``, not an exception and not an empty facet — see
    :class:`RiskTransferFacet` on why an empty block is worse than no block.
    """
    if actuarial is None and incident_loss is None:
        return None
    return RiskTransferFacet(actuarial=actuarial, incident_loss=incident_loss)


def attach_facet(
    capsule: dict[str, Any], facet: RiskTransferFacet | None
) -> dict[str, Any]:
    """Attach the risk-transfer facet to a capsule dict, additively.

    Writes nothing when there is no facet: a run with no risk-transfer
    material must be byte-identical to one captured before this feature
    existed (I-3). Returns a new dict; the input is not mutated.

    ``exclude_none`` drops unset optional fields but never ``declared_by`` or
    ``source``, which are required — a loss item's provenance cannot be
    optimised out of the sealed record (I-4).
    """
    if facet is None:
        return capsule
    out = dict(capsule)
    facets = dict(out.get("facets") or {})
    facets[FACET_NAME] = facet.model_dump(exclude_none=True)
    out["facets"] = facets
    return out


__all__ = [
    "FACET_NAME",
    "SCHEMA_VERSION",
    "ActuarialBlock",
    "FloatAmountRejectedError",
    "IncidentLoss",
    "InvalidReferenceError",
    "LossFeature",
    "LossFeatureKind",
    "LossItem",
    "LossSource",
    "MissingDeclaredByError",
    "MissingIncidentBundleError",
    "Money",
    "PaymentSecretRejectedError",
    "RiskTransferFacet",
    "UnquantifiedFeatureError",
    "attach_facet",
    "build_actuarial",
    "build_facet",
    "build_incident_loss",
    "digest_artifact",
    "extract_loss_features",
    "is_measured",
    "verify_ref_binding",
]
