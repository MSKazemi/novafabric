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

"""Injection / jailbreak attempts — ADR-0145 D2/P2 (NF-132, NF-133).

An adversarial attempt is recorded as **where it was and what it hashed to**,
never as what it said. The payload is present only as ``payload_digest``; the
tainted region is located by a :class:`SourceSpan` naming the ingested
artifact by ``content_hash``.

The four ADR-0145 invariants apply unchanged, and two of them do most of the
work here:

- **I-3 No payloads.** An injection or jailbreak payload is, by definition,
  attacker-controlled text. Re-storing it inside evidence that later travels
  into exports, dashboards and support tickets is itself the hazard ADR-0145
  alternative #5 rejects. This module therefore has *no* free-prose field at
  all (see :class:`AdversarialAttempt`), and structurally refuses payload text
  offered under any name — the caller gets
  :class:`RawAttemptTextRejectedError`, never a silently-computed digest.
- **I-4 Verdict-not-adjudicated.** An attempt record says a named detector
  flagged something, and what the application's guardrail then did with it.
  It never says the attempt was malicious, that it succeeded, or that it was
  stopped. ``injection_class``, ``verdict``, ``technique`` and ``disposition``
  are all *reported* values carried with attribution.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable, Mapping
from typing import Any, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from novafabric.safety._primitives import DIGEST_RE, DetectorProvenance, digest_bytes

#: Field names that name adversarial text rather than a reference to it.
#:
#: Anchored per token so ``payload_digest``, ``prompt_digest`` and
#: ``text_span`` survive while a bare ``payload``, ``prompt`` or ``text`` does
#: not. This catches the caller who put the attack string under an honest
#: name; the value checks below catch the one who hid it under a bland one.
_ATTEMPT_TEXT_KEY_RE = re.compile(
    r"^(payload|prompt|prompts|text|raw|body|content|message|attack|"
    r"attack_string|injection|jailbreak|snippet|excerpt|sample|quote|"
    r"matched_text|matched|input|output|completion|transcript)$"
    r"|_(text|payload|prompt|string|snippet|excerpt|transcript)$",
    re.IGNORECASE,
)

#: Length above which a string in an attempt record is treated as payload text.
#:
#: A judgement call the ADR does not settle — it says "never the raw payload"
#: without saying how a validator recognises one. Every value this object
#: legitimately holds is an identifier, a label, or a digest, and the longest
#: of those is a ``sha256:`` digest at 71 characters; 256 leaves ~3.5x headroom
#: for operator-chosen attempt ids and rule ids (the same bound, for the same
#: reason, as the ADR-0162 embodied facet). Above it, a free-form string in a
#: record about an attack is overwhelmingly the attack. The cap is why this
#: module ships no ``reason`` field: a prose explanation of an injection tends
#: to quote the injection, so the labelled fields are the only channel offered.
_MAX_LABEL_LEN = 256


class RawAttemptTextRejectedError(Exception):
    """Raised when adversarial payload text is offered to an attempt record.

    Deliberately **not** a ``ValueError``. Pydantic v2 folds a ``ValueError``
    raised inside a validator into a ``ValidationError`` alongside ordinary
    shape complaints, destroying the named type — and this is the one mistake
    a caller most needs told apart from a typo. Someone who passed the attack
    string itself has made a *specific* error with privacy and re-exposure
    consequences (ADR-0145 I-3, ADR-0021 §4, ADR-0009), and silently hashing
    it for them would teach that passing payloads is fine.

    Names the field and the rule that fired — never the value, since that
    value is the payload this exception exists to keep out of logs as well as
    out of capsules.
    """

    def __init__(self, path: str, rule: str) -> None:
        super().__init__(
            f"field {path or '<root>'!r} carries adversarial payload text "
            f"({rule}); an injection/jailbreak record holds digests, spans and "
            "labels only — hash the payload with digest_payload() and keep the "
            "text out of the capsule (ADR-0145 D2/I-3, ADR-0021 §4)"
        )
        self.path = path
        self.rule = rule


class InvalidDigestError(Exception):
    """Raised when a digest-shaped reference is malformed.

    Not a ``ValueError``, for the reason given on
    :class:`RawAttemptTextRejectedError`. Kept distinct from it so a truncated
    or upper-case digest is reported as the typo it is, rather than accusing
    the caller of leaking a payload.
    """


class EmptySpanError(Exception):
    """Raised when a source span locates nothing.

    A span with neither byte offsets nor a chunk reference names an artifact
    but no region of it, which is indistinguishable from "somewhere in this
    document" — provenance the reader cannot act on, presented as if it were
    precise.
    """


def digest_payload(payload: str | bytes) -> str:
    """Return the ``sha256:`` digest of an adversarial payload.

    The **only** function here that accepts payload text, and it retains none
    of it. It exists so a caller has a correct way to produce a
    ``payload_digest`` — the alternative being a caller who reaches for the
    attempt record and passes the string straight in.

    Identical construction to :func:`novafabric.safety.decisions.digest_inputs`
    — both delegate to the one hash helper — so a verifier never has to know
    which half of the facet wrote a digest.
    """
    return digest_bytes(payload)


# ── The payload-text boundary (I-3) ───────────────────────────────────────


def _check_scalar(value: Any, path: str) -> None:
    """Raise if a single value is (or plausibly is) adversarial payload text."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        raise RawAttemptTextRejectedError(path, f"{type(value).__name__} buffer")
    if isinstance(value, str) and len(value) > _MAX_LABEL_LEN:
        raise RawAttemptTextRejectedError(
            path, f"free-form string of {len(value)} characters"
        )


def reject_attempt_text(value: Any, *, path: str = "") -> None:
    """Walk ``value`` and raise on the first adversarial payload found (I-3).

    Walks keys as well as values, and descends into nested models: a payload
    can arrive as a long string under a harmless name, or as a short string
    under a name that announces what it is (``{"payload": "x"}`` trips the key
    rule and should — a one-character payload field is still a payload field,
    and the next caller's will not be one character).

    Raises:
        RawAttemptTextRejectedError: naming the field and the rule that fired,
            never the value.
    """
    if isinstance(value, BaseModel):
        reject_attempt_text(_own_fields(value), path=path)
        return
    if isinstance(value, Mapping):
        for key, child in value.items():
            name = str(key)
            child_path = f"{path}.{name}" if path else name
            if _ATTEMPT_TEXT_KEY_RE.search(name):
                raise RawAttemptTextRejectedError(child_path, "payload-named field")
            reject_attempt_text(child, path=child_path)
        return
    if isinstance(value, (list, tuple, set, frozenset)):
        for index, item in enumerate(value):
            reject_attempt_text(item, path=f"{path}[{index}]")
        return
    _check_scalar(value, path)


def _own_fields(model: BaseModel) -> dict[str, Any]:
    """Declared fields plus ``extra`` ones, with values un-serialised.

    Used instead of ``model_dump()`` because dumping is precisely what must
    not be attempted on an unrecognised object: pydantic would warn or coerce,
    and the value most in need of inspection is the one it cannot serialise.
    """
    return {**model.__dict__, **(model.__pydantic_extra__ or {})}


def _validate_digest(value: str, *, path: str) -> str:
    """Accept only a canonical ``sha256:<64 hex>`` string in a digest field."""
    if DIGEST_RE.match(value):
        return value
    # A value that at least *tried* to be a digest is a typo; anything else is
    # a caller handing over the payload. Reporting the two the same way would
    # either accuse an honest typo of leaking, or let a real leak read as one.
    if value.lower().startswith(("sha256:", "sha-256:", "sha256-")):
        raise InvalidDigestError(
            f"{path} is not a canonical 'sha256:<64 lowercase hex>' digest; "
            "produce it with digest_payload() (ADR-0145 D2)"
        )
    raise RawAttemptTextRejectedError(path, "non-digest value in a digest field")


# ── Models ────────────────────────────────────────────────────────────────

#: Where the tainted region sits. ``direct`` = the user turn itself;
#: ``indirect`` = content the agent ingested (a retrieved document, a tool
#: result, a scraped page), which is the OWASP LLM01 direct-vs-indirect split.
InjectionClass = Literal["direct", "indirect"]

#: The detector's verdict, recorded with attribution and never endorsed (I-4).
#: ``benign`` is a real and useful record: a detector that looked and found
#: nothing is evidence the control ran.
JailbreakVerdict = Literal["benign", "suspected", "confirmed"]

#: What the application's guardrail did. Mirrors
#: :data:`novafabric.safety.decisions.Disposition` rather than importing it,
#: to keep the import edge one-directional (decisions imports this module).
AttemptDisposition = Literal["allow", "block", "rewrite"]


class SourceSpan(BaseModel):
    """Where the adversarial content sat in the input (D2).

    ``artifact_ref`` is the ingested artifact's ``content_hash`` — the link
    that lets a reader trace *which retrieved document carried this*. The
    region within it is given either as byte offsets or as an opaque
    ``chunk_ref`` (retrieval pipelines that chunk before the agent sees bytes
    often have no offset to give); at least one must be present, or the span
    names a document and no place in it.
    """

    model_config = ConfigDict(extra="allow")

    #: ``sha256:`` content hash of the ingested artifact. Optional: a direct
    #: injection typed by the user arrived in no artifact at all, and inventing
    #: a ref for it would be a claimed linkage (see ``unbound``).
    artifact_ref: str | None = None
    byte_start: int | None = Field(default=None, ge=0)
    byte_end: int | None = Field(default=None, ge=0)
    #: Retrieval-pipeline chunk identifier, when byte offsets are unavailable.
    chunk_ref: str | None = None

    @field_validator("artifact_ref", mode="before")
    @classmethod
    def _check_artifact_ref(cls, value: Any) -> Any:
        if value is None:
            return None
        if not isinstance(value, str):
            reject_attempt_text(value, path="artifact_ref")
            return value
        return _validate_digest(value, path="artifact_ref")

    @model_validator(mode="after")
    def _require_a_locator(self) -> SourceSpan:
        has_offsets = self.byte_start is not None and self.byte_end is not None
        if not has_offsets and not self.chunk_ref:
            raise EmptySpanError(
                "source span carries neither byte offsets nor a chunk_ref; a "
                "span that locates no region is not provenance (ADR-0145 D2)"
            )
        if has_offsets:
            # mypy: both are non-None inside this branch.
            assert self.byte_start is not None and self.byte_end is not None
            if self.byte_end < self.byte_start:
                raise EmptySpanError(
                    f"source span byte_end ({self.byte_end}) precedes byte_start "
                    f"({self.byte_start}); the span cannot be resolved"
                )
        reject_attempt_text(_own_fields(self))
        return self


class AdversarialAttempt(BaseModel):
    """Fields shared by injection (NF-132) and jailbreak (NF-133) attempts.

    Deliberately has **no** free-prose field. ADR-0145 gives the decision
    object a ``reason``; it gives attempts none, and adding one here would
    reopen exactly the channel D2 closes — prose about an attack quotes the
    attack. ``rule_id`` and the labelled enums carry the same information in a
    form that cannot smuggle a payload.
    """

    model_config = ConfigDict(extra="allow")

    attempt_id: str
    #: sha256 over the exact adversarial payload. The payload is not stored.
    payload_digest: str
    source_span: SourceSpan | None = None
    #: What the application's guardrail did about it, if it said. Absent means
    #: "no disposition was recorded", never "it was allowed" — the same
    #: absent-is-not-false stance the decision object takes.
    disposition: AttemptDisposition | None = None
    detector: DetectorProvenance | None = None
    score: float | None = None
    rule_id: str | None = None
    detected_at: str
    #: NF-132/133 require every attempt to map to OWASP LLM01. This is taxonomy
    #: placement, not a judgement about the attempt (I-4). The *aggregated*
    #: crosswalk view over a whole capsule is NF-135/P4 and is not built here.
    crosswalk: list[str] = Field(default_factory=lambda: ["LLM01"])
    #: True when no ingested artifact is linked, or the linked one did not
    #: resolve. About the *linkage*, not the attempt: an unbound attempt was
    #: still recorded, NovaFabric simply holds no artifact it can point to.
    #: Recorded, never fatal, never dropped.
    unbound: bool = True

    @field_validator("payload_digest", mode="before")
    @classmethod
    def _check_payload_digest(cls, value: Any) -> Any:
        if not isinstance(value, str):
            # bytes here is the classic mistake: `payload_digest=open(...).read()`.
            reject_attempt_text(value, path="payload_digest")
            return value
        return _validate_digest(value, path="payload_digest")

    @model_validator(mode="after")
    def _enforce_boundary(self) -> AdversarialAttempt:
        """No resolved artifact ⇒ ``unbound``; and no payload text anywhere.

        Both are enforced in the model rather than only in the builders, so
        that ``model_validate`` of untrusted JSON, ``model_copy(update=…)`` and
        direct construction cannot produce a record claiming a linkage it does
        not have, or one carrying the payload. An attempt marked
        ``unbound: false`` with no ``artifact_ref`` would read, to anyone
        tracing which document poisoned a run, as a *confirmed* provenance
        chain — the most misleading field combination this module could allow.
        """
        if self.source_span is None or self.source_span.artifact_ref is None:
            self.unbound = True
        reject_attempt_text(_own_fields(self))
        return self


class InjectionAttempt(AdversarialAttempt):
    """A prompt-injection attempt a detector flagged (NF-132)."""

    #: ``direct`` vs ``indirect`` per OWASP LLM01. Reported by the detector and
    #: recorded with attribution; NovaFabric does not classify (I-4).
    injection_class: InjectionClass


class JailbreakAttempt(AdversarialAttempt):
    """A jailbreak attempt a detector flagged (NF-133)."""

    verdict: JailbreakVerdict
    #: ``role_play``, ``crescendo``, ``encoding``, … Left free-form because the
    #: spec gives these as examples and the technique taxonomy moves faster
    #: than this schema; a closed set would force a novel technique into the
    #: wrong bucket, which is a worse evidential outcome than an unfamiliar
    #: label the consumer can map.
    technique: str | None = None


# ── Linkage resolution ────────────────────────────────────────────────────

AttemptT = TypeVar("AttemptT", bound=AdversarialAttempt)


def resolve_artifact_linkage(
    attempt: AttemptT, known_content_hashes: Collection[str]
) -> AttemptT:
    """Return the attempt with ``unbound`` set from a resolvable artifact.

    ``unbound`` clears only when the span names an artifact *and* that
    artifact is among the hashes the caller can actually resolve. An attempt
    referencing a document nobody holds stays ``unbound``: naming a hash is
    not the same as being able to show the reader the document, and ADR-0145
    would rather record a gap than assert a chain it cannot support.

    Returns a copy; the input is not mutated.
    """
    span = attempt.source_span
    # The `is not None` is redundant against a well-typed collection and kept
    # anyway: `model_copy` does not re-run the model validator, so this line is
    # the only thing standing between a `None` slipping into the caller's set
    # and a record that claims a linkage to no artifact at all.
    resolved = (
        span is not None
        and span.artifact_ref is not None
        and span.artifact_ref in set(known_content_hashes)
    )
    return attempt.model_copy(update={"unbound": not resolved})


def verify_payload_binding(attempt: AdversarialAttempt, payload: str | bytes) -> bool:
    """Re-verify an attempt's digest against a payload held elsewhere.

    For the auditor who *does* hold the quarantined payload out-of-band and
    needs to confirm this record is about it. Mirrors
    ``verify_decision_binding``: it answers "is this the same bytes", never
    "was this malicious" (I-4).
    """
    return attempt.payload_digest == digest_payload(payload)


# ── Assembly ──────────────────────────────────────────────────────────────


def build_injection(
    *,
    attempt_id: str,
    injection_class: InjectionClass,
    payload_digest: str,
    detected_at: str,
    source_span: SourceSpan | None = None,
    disposition: AttemptDisposition | None = None,
    detector: DetectorProvenance | None = None,
    score: float | None = None,
    rule_id: str | None = None,
    known_content_hashes: Collection[str] = (),
) -> InjectionAttempt:
    """Assemble an injection attempt, resolving its artifact linkage.

    Args:
        payload_digest: produced by :func:`digest_payload`. Passing the payload
            itself raises :class:`RawAttemptTextRejectedError`.
        known_content_hashes: artifact hashes the caller can resolve; anything
            outside it leaves the record ``unbound``.

    Raises:
        RawAttemptTextRejectedError: if any argument carries payload text.
        InvalidDigestError: if a digest is malformed.
        EmptySpanError: if the span locates no region.
    """
    attempt = InjectionAttempt(
        attempt_id=attempt_id,
        injection_class=injection_class,
        payload_digest=payload_digest,
        source_span=source_span,
        disposition=disposition,
        detector=detector,
        score=score,
        rule_id=rule_id,
        detected_at=detected_at,
    )
    return resolve_artifact_linkage(attempt, known_content_hashes)


def build_jailbreak(
    *,
    attempt_id: str,
    verdict: JailbreakVerdict,
    payload_digest: str,
    detected_at: str,
    technique: str | None = None,
    source_span: SourceSpan | None = None,
    disposition: AttemptDisposition | None = None,
    detector: DetectorProvenance | None = None,
    score: float | None = None,
    rule_id: str | None = None,
    known_content_hashes: Collection[str] = (),
) -> JailbreakAttempt:
    """Assemble a jailbreak attempt, resolving its artifact linkage.

    See :func:`build_injection` for the argument and error contract.
    """
    attempt = JailbreakAttempt(
        attempt_id=attempt_id,
        verdict=verdict,
        technique=technique,
        payload_digest=payload_digest,
        source_span=source_span,
        disposition=disposition,
        detector=detector,
        score=score,
        rule_id=rule_id,
        detected_at=detected_at,
    )
    return resolve_artifact_linkage(attempt, known_content_hashes)


def sort_attempts(attempts: Iterable[AttemptT]) -> list[AttemptT]:
    """Order attempts by detection time, as the facet stores them."""
    return sorted(attempts, key=lambda a: a.detected_at)
