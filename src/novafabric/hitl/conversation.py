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

"""Conversation-thread provenance — ADR-0150 D1/P1 (NF-181).

Threads the human<->agent collaboration that produced a decision, so every
later accountability facet (decision-context receipt, override, rationale,
handoff, acted-on-behalf) can anchor to a specific ``turn_id`` instead of to a
point-in-time approval that carries no conversation around it.

This is *provenance, not transcript storage*. The facet records that a human
said something, when, and under which pseudonymous identity — never what they
said. Turn content enters only as a ``sha256:`` digest.

Four invariants from ADR-0150 D7 / the NF-181-190 spec §3 shape every choice
here:

- **I-1 Additive-first.** The facet lives in optional ``facets.conversation``.
  A capsule with no conversation material is returned byte-identical to one
  captured before this feature existed.
- **I-2 Pseudonymous, digest-only.** Authors are identity refs
  (``human:did:…``, ``human:fp:<hex>``, ``agent:spiffe://…``) — never a name,
  an email, or free-text PII (ADR-0009, ADR-0021 §4). Turn content is bound by
  digest; raw bytes offered to a digest field are an error, not something to
  silently hash.
- **I-3 Fail-open.** Absent material means no facet, never an exception, never
  a blocked workload. Malformed material is a different thing and does raise —
  see :class:`DuplicateTurnError`.
- **I-4 Record-only.** The thread records what was said by whom. It never
  asserts that oversight occurred, was adequate, or was lawful; a missing
  attribution is *unknown*, never a verdict either way.

P1 is the thread model and turn-ref resolution only. The decision-context
receipt (NF-182), override (NF-187), rationale (NF-188) and handoff (NF-189)
are later slices and deliberately absent — the models' ``extra="allow"`` config
is what lets them extend a turn without a schema break.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Sequence
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

FACET_NAME = "conversation"
SCHEMA_VERSION = "0.1.0"

Role = Literal["human", "agent", "system"]

#: The one digest form the rest of the capsule uses. Matched strictly
#: (lower-case hex, exact length) so a truncated or upper-cased digest fails
#: loudly here rather than failing to match at verify time, months later, in an
#: audit.
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

#: An identity ref is scheme-prefixed and opaque: `human:did:…`,
#: `human:fp:<sha256[:16]>`, `agent:spiffe://…`, `system:<component>`. The
#: scheme is required because it is what makes the ref *recognisably* a
#: pseudonym — a bare `alice` would sail through any shape check while being
#: exactly the raw-identity capture I-2 forbids.
_IDENTITY_RE = re.compile(r"^(?:human|agent|system):\S{3,}$")

#: An identity ref is an identifier, never a document. Anything longer is
#: overwhelmingly likely to be inlined content someone routed through an
#: attribution field.
MAX_REF_LENGTH = 512

#: role -> the identity-ref scheme that role's author must carry.
_ROLE_SCHEME: dict[str, str] = {
    "human": "human:",
    "agent": "agent:",
    "system": "system:",
}


# ── Errors ────────────────────────────────────────────────────────────────
#
# All subclass Exception rather than ValueError: Pydantic v2 folds a ValueError
# raised inside a validator into a ValidationError, which destroys the named
# type and with it the caller's ability to tell an I-2 violation from a typo.


class ConversationError(Exception):
    """Base for every conversation-provenance error."""


class IdentityRefError(ConversationError):
    """Raised when an author is not a pseudonymous identity ref (I-2)."""


class TurnContentError(ConversationError):
    """Raised when turn *content* is offered where a content digest belongs.

    Distinct from :class:`IdentityRefError` on purpose: a malformed digest is a
    caller mistake, while turn text arriving at this boundary is the I-2
    violation the whole facet exists to prevent, and the two want different
    fixes from whoever reads the traceback.
    """


class TurnTimeError(ConversationError):
    """Raised when a turn timestamp cannot be parsed.

    Turns are ordered by time; an unparseable timestamp would silently misorder
    the thread, and a misordered thread reads as a different conversation.
    """


class DuplicateTurnError(ConversationError):
    """Raised when two turns share a ``turn_id``.

    Not softened to a warning despite I-3's fail-open posture: fail-open covers
    *absent* material, and a duplicate id makes every downstream ``turn_ref``
    ambiguous — the resolution silently picks one turn and the evidence points
    at the wrong moment in the conversation.
    """


# ── Validation helpers ────────────────────────────────────────────────────


def _validate_identity_ref(value: object, *, field: str) -> str:
    """Return ``value`` if it is an acceptable pseudonymous identity ref."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        raise IdentityRefError(f"{field} must be a text identity ref, not raw bytes")
    if not isinstance(value, str):
        raise IdentityRefError(f"{field} must be a string identity ref")
    if len(value) > MAX_REF_LENGTH:
        raise IdentityRefError(
            f"{field} is {len(value)} chars, over the {MAX_REF_LENGTH}-char "
            "reference limit; this looks like inlined content, not an identity ref"
        )
    # Checked ahead of the shape regex so the error names the actual hazard:
    # `human:alice@example.com` is well-formed by shape and is still raw PII.
    if "@" in value:
        raise IdentityRefError(
            f"{field} contains '@' and looks like an email address; identities "
            "must be pseudonymous refs (ADR-0021 §4, ADR-0009 — no raw PII in "
            "the capsule). Use human:fp:<sha256[:16]> or a DID."
        )
    if not _IDENTITY_RE.match(value):
        raise IdentityRefError(
            f"{field} must be a scheme-prefixed pseudonymous ref "
            f"('human:…', 'agent:…', 'system:…'), got {value!r}"
        )
    return value


def _validate_content_digest(value: object, *, field: str) -> str:
    """Return ``value`` if it is a ``sha256:`` digest of turn content."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        raise TurnContentError(
            f"{field} must be a sha256 digest, not raw bytes; digest the turn "
            "yourself with digest_turn() and pass the result (ADR-0150 D1 — the "
            "capsule never holds turn content)"
        )
    if not isinstance(value, str):
        raise TurnContentError(f"{field} must be a 'sha256:<64 hex>' string")
    if not _DIGEST_RE.match(value):
        # Deliberately does not echo `value` back: if the caller passed the turn
        # text by mistake, echoing it would write the very content this field
        # exists to keep out into the log the exception lands in.
        raise TurnContentError(
            f"{field} must be 'sha256:<64 hex>' (got a {len(value)}-char string "
            "that is not a digest — if this is turn text, hash it with "
            "digest_turn(); turn content never enters the capsule)"
        )
    return value


def _parse_at(value: str, *, field: str) -> datetime:
    """Parse an ISO-8601 turn timestamp into an aware datetime for ordering."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise TurnTimeError(
            f"{field} must be an ISO-8601 timestamp, got {value!r}"
        ) from exc
    # A naive timestamp is read as UTC rather than rejected: capture sites that
    # already emit naive UTC exist, and rejecting them would make a whole thread
    # unrecordable (I-3) over a formatting detail. Comparing naive against aware
    # raises, so normalising here is what keeps ordering total.
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def digest_turn(content: str | bytes) -> str:
    """Return the ``sha256:`` digest of one turn's rendered content.

    Callers hash the turn and put only the result in the capsule (I-2).
    Emitted in the same ``sha256:<hex>`` form the rest of the capsule uses, so
    a verifier does not have to know which subsystem wrote it.
    """
    raw = content.encode("utf-8") if isinstance(content, str) else content
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


# ── Objects ───────────────────────────────────────────────────────────────


class Turn(BaseModel):
    """One human, agent, or system turn in the collaboration (NF-181)."""

    model_config = ConfigDict(extra="allow")

    turn_id: str
    author: str
    role: Role
    #: sha256 over the rendered turn. Required, not optional: a turn is always
    #: hashable by whoever captured it, and a turn with no binding would be an
    #: attribution claim with nothing to check — the shape most likely to be
    #: mistaken for evidence in an audit.
    content_digest: str
    #: None for a thread root. Absent parent = "this started the thread", which
    #: is why it is not an error; a parent naming a turn that is not in the
    #: facet is a different case, surfaced by `broken_parent_refs`.
    parent_turn_id: str | None = None
    at: str

    # mode="before" on the ref fields: Pydantic's own str coercion would reject
    # bytes first, with a generic "not a valid string" that tells the caller
    # nothing about *why* bytes are forbidden here (I-2).
    @field_validator("author", mode="before")
    @classmethod
    def _check_author(cls, v: object) -> str:
        return _validate_identity_ref(v, field="author")

    @field_validator("content_digest", mode="before")
    @classmethod
    def _check_content_digest(cls, v: object) -> str:
        return _validate_content_digest(v, field="content_digest")

    @field_validator("turn_id", "parent_turn_id")
    @classmethod
    def _check_turn_id(cls, v: str | None) -> str | None:
        if v is not None and not v.strip():
            raise ConversationError(
                "turn ids must be non-empty; an empty id cannot be resolved by a "
                "later facet's turn_ref (NF-181 spec §3.5)"
            )
        return v

    @field_validator("at")
    @classmethod
    def _check_at(cls, v: str) -> str:
        # Parsed for validation, but the *original* string is stored: the
        # capsule is sealed over these bytes, and normalising a caller's
        # timestamp would change what they sealed for a cosmetic gain.
        _parse_at(v, field="at")
        return v

    @model_validator(mode="after")
    def _check_role_matches_author_scheme(self) -> Turn:
        """A `human` role with an `agent:` author is a misattribution.

        The single fact this facet exists to establish is *who said this*. A
        role and an author scheme that disagree make that fact unreadable, and
        an oversight auditor counting human turns would silently count wrong.
        """
        expected = _ROLE_SCHEME[self.role]
        if not self.author.startswith(expected):
            raise IdentityRefError(
                f"turn {self.turn_id!r} has role {self.role!r} but author "
                f"{self.author!r}; a {self.role} turn must carry a "
                f"{expected!r} identity ref"
            )
        return self

    @property
    def at_time(self) -> datetime:
        """The turn's timestamp as an aware datetime, for ordering."""
        return _parse_at(self.at, field="at")


class ConversationFacet(BaseModel):
    """The optional ``facets.conversation`` block (NF-181, I-1)."""

    model_config = ConfigDict(extra="allow")

    schema_version: str = SCHEMA_VERSION
    #: Back-reference to the ADR-0122 session capsule this thread belongs to.
    #: Optional: a single-run conversation has no session, and requiring one
    #: would make the facet unusable before the session capsule ships.
    session_ref: str | None = None
    turns: list[Turn] = Field(default_factory=list)

    @field_validator("session_ref", mode="before")
    @classmethod
    def _check_session_ref(cls, v: object) -> str | None:
        if v is None:
            return None
        return _validate_content_digest(v, field="session_ref")

    @property
    def has_material(self) -> bool:
        """True when the facet records at least one turn.

        A facet with a ``session_ref`` and no turns is not material: it adds a
        block, a schema version and a seal surface while answering none of the
        questions NF-181 exists to answer.
        """
        return bool(self.turns)


# ── Construction ──────────────────────────────────────────────────────────


def turn(
    turn_id: str,
    author: str,
    role: Role,
    *,
    content: str | bytes | None = None,
    content_digest: str | None = None,
    parent_turn_id: str | None = None,
    at: str,
) -> Turn:
    """Build one turn, from either the rendered content or its digest.

    ``content`` is hashed here and immediately discarded — it is a convenience
    for capture sites that hold the turn in memory, and it is the *only* path
    by which turn text may touch this module.

    Raises:
        TurnContentError: if neither or both of ``content`` / ``content_digest``
            are given. Both is ambiguous (which one binds?), and neither leaves
            the turn unbound, which :class:`Turn` does not permit.
    """
    if (content is None) == (content_digest is None):
        raise TurnContentError(
            "pass exactly one of content= (hashed here, not stored) or "
            "content_digest= (already hashed by the caller)"
        )
    digest = content_digest if content is None else digest_turn(content)
    return Turn(
        turn_id=turn_id,
        author=author,
        role=role,
        content_digest=digest,  # type: ignore[arg-type]
        parent_turn_id=parent_turn_id,
        at=at,
    )


def build_facet(
    turns: Iterable[Turn],
    *,
    session_ref: str | None = None,
) -> ConversationFacet:
    """Assemble the conversation facet, ordering turns chronologically.

    Ordering is a *stable* sort on the turn timestamp alone. Two turns can
    share a timestamp at second granularity, and when they do the caller's
    order is the causal order they were observed in — better evidence than any
    tie-break this module could invent. Sorting by ``turn_id`` as a tie-break
    was rejected: it is lexical, so it would place ``t10`` before ``t2`` and
    reorder a thread on nothing but the digits in its ids.

    Raises:
        DuplicateTurnError: if two turns share a ``turn_id`` (see the class).
    """
    ordered = sorted(turns, key=lambda t: t.at_time)
    seen: set[str] = set()
    for item in ordered:
        if item.turn_id in seen:
            raise DuplicateTurnError(
                f"turn_id {item.turn_id!r} appears more than once; every later "
                "facet's turn_ref would resolve ambiguously (NF-181 spec §3.5)"
            )
        seen.add(item.turn_id)
    return ConversationFacet(session_ref=session_ref, turns=ordered)


def attach_facet(
    capsule: dict[str, Any], facet: ConversationFacet
) -> dict[str, Any]:
    """Attach the conversation facet to a capsule dict, additively.

    Writes nothing when the facet records no turns: a run with no conversation
    material must be byte-identical to one captured before this feature existed
    (I-1, I-3). Returns a new dict; the input is not mutated.
    """
    if not facet.has_material:
        return capsule
    out = dict(capsule)
    facets = dict(out.get("facets") or {})
    # exclude_none so an absent `parent_turn_id` is *absent*, not `null`: a
    # thread root and a turn whose parent was dropped must not serialise
    # identically (I-4 — absent is unknown, not a claim).
    facets[FACET_NAME] = facet.model_dump(exclude_none=True)
    out["facets"] = facets
    return out


def facet_from_capsule(capsule: dict[str, Any]) -> ConversationFacet | None:
    """Read the conversation facet back out of a capsule dict.

    Returns None when the capsule has no facet — the overwhelmingly common
    case, and not an error (I-3).
    """
    facets = capsule.get("facets")
    if not isinstance(facets, dict):
        return None
    block = facets.get(FACET_NAME)
    if not isinstance(block, dict):
        return None
    return ConversationFacet.model_validate(block)


# ── Resolution and verification ───────────────────────────────────────────


def resolve_turn(facet: ConversationFacet, turn_id: str) -> Turn | None:
    """Resolve a ``turn_ref`` against the thread.

    Returns None for an unknown id rather than raising: the caller — a later
    accountability facet, or `nova hitl thread show` — is the one that decides
    whether a dangling ref is fatal in its context (I-4).
    """
    for item in facet.turns:
        if item.turn_id == turn_id:
            return item
    return None


def dangling_turn_refs(
    facet: ConversationFacet, refs: Sequence[str]
) -> list[str]:
    """Return the ``turn_ref`` values that do not resolve to a turn.

    The spec's normative requirement 5: every other facet's ``turn_ref`` must
    resolve into this list, and a dangling one must be *flagged*. Flagging is
    all this does — it does not repair the thread and does not decide that the
    evidence is invalid (I-4).

    Duplicates in ``refs`` are preserved so a caller can report per-occurrence.
    """
    known = {item.turn_id for item in facet.turns}
    return [ref for ref in refs if ref not in known]


def broken_parent_refs(facet: ConversationFacet) -> list[str]:
    """Return the ids of turns whose ``parent_turn_id`` does not resolve.

    A root turn (``parent_turn_id`` absent) is not broken — it is where the
    conversation started. A turn naming itself as its parent *is* broken: it is
    a one-node cycle that would hang any thread walker, and it can only be a
    capture bug.
    """
    known = {item.turn_id for item in facet.turns}
    broken: list[str] = []
    for item in facet.turns:
        parent = item.parent_turn_id
        if parent is None:
            continue
        if parent == item.turn_id or parent not in known:
            broken.append(item.turn_id)
    return broken


def verify_turn_binding(item: Turn, content: str | bytes) -> bool:
    """Re-verify a turn's digest against the content it claims to bind.

    The offline half of the NF-181 guarantee: given the rendered turn (held by
    whoever ran the conversation, never by the capsule), an auditor can confirm
    the sealed thread refers to exactly that text.
    """
    return item.content_digest == digest_turn(content)
