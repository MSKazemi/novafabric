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

"""A2A message envelopes — ADR-0142 D1/P1 (NF-101).

A2A v1.0 runs the exchange between agents and seals *identity* (the signed
Agent Card), but it persists no durable record of the conversation. This module
produces that record: each agent→agent ``Message`` becomes a content-addressed,
card-bound **envelope** — public ids, a ``sha256:`` digest of the message parts,
the sender's card fingerprint, and the signature — and nothing else.

Four invariants from ADR-0142 / NF-101…110 §3 shape every choice here:

- **I-1 Additive-first.** Envelopes live in the optional ``facets.a2a_messages``
  block. A single-agent capsule with no A2A material is returned unchanged, and
  :func:`attach_facet` never mutates its input.
- **I-2 Reference-only; no payloads.** A message body is represented by its
  ``content_hash`` alone. There is no code path in this module that stores
  message bytes: ``blob_ref`` is typed ``None``, so opt-in byte capture (P6,
  ``--capture-a2a-bodies``) cannot be smuggled in early.
- **I-3 Fail-open, and absent is not false.** An unsigned or unfetchable card
  leaves ``card_verified`` at ``None`` — "not checked" — never ``False``. A
  missing signature is unknown provenance, not a failed verification.
- **I-4 Record, do not adjudicate.** An envelope records *what was on the wire*.
  It never asserts the exchange was correct, authorised, or injection-free.

**Ordering is not causality.** :func:`build_facet` sorts envelopes by
``sent_at`` purely so the serialised facet is deterministic. Wall-clock order
across agents is *not* a happens-before claim; the honest partial order comes
from vector clocks in NF-105 (P3). ``vclock`` is carried through here so a P3
verifier has it, but nothing in P1 interprets it.

**Schema note.** ADR-0142 places these objects under a capsule ``facets`` key,
matching the sibling ADR-0145 slice. ``schemas/run-capsule.schema.json`` does
not yet declare ``facets`` (and is ``additionalProperties: false``), so a
facet-bearing *manifest* is not yet schema-valid; declaring it is a shared-schema
change outside this slice. The P1 additive proof is therefore the one that
matters and does hold: a capsule with no A2A material passes through untouched
and still validates.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

FACET_NAME = "a2a_messages"
SCHEMA_VERSION = "0.1.0"

#: The A2A revision these envelopes are shaped against. Recorded on the facet
#: (NF-101…110 §3 R16) so a future parser knows which spec produced the record
#: rather than having to guess from the field set.
A2A_VERSION = "1.0"

#: A2A `Message.role`. The wire names are used verbatim — a NovaFabric-local
#: renaming would make the record harder, not easier, to check against a trace.
A2ARole = Literal["ROLE_USER", "ROLE_AGENT"]

PartKind = Literal["text", "file", "data"]

CardStatus = Literal["verified", "unverified", "unknown"]

#: Card fields excluded from the fingerprint. The signature is *over* the card;
#: including it would make the same logical card fingerprint differently each
#: time it is re-signed, so "did this agent present the same card" — the
#: question the fingerprint exists to answer — would become unanswerable.
#: Everything else is included: fingerprinting a hand-picked subset would leave
#: the remaining fields free to vary undetected.
_UNFINGERPRINTED_CARD_KEYS = frozenset({"signature", "signatures"})


# ── Errors ────────────────────────────────────────────────────────────────


class A2AEvidenceError(ValueError):
    """Base class for A2A evidence construction failures."""


class EmptyMessagePartsError(A2AEvidenceError):
    """Raised when a content hash is requested over zero message parts."""


class CardBindingError(A2AEvidenceError):
    """Raised when a card verdict is claimed without the card it is about."""


class DuplicateMessageIdError(A2AEvidenceError):
    """Raised when a facet would carry two envelopes with the same ``msg_id``."""


# ── Content binding ───────────────────────────────────────────────────────


class MessagePart(BaseModel):
    """One A2A ``Message`` part — **an input to hashing, never stored**.

    Deliberately not a field of :class:`A2AMessageEnvelope`. Parts carry the
    actual prompt/response content; they exist only long enough to compute
    :func:`content_hash_for_parts`, and the envelope keeps the digest (I-2).
    """

    model_config = ConfigDict(extra="allow")

    kind: PartKind
    text: str | None = None
    data: dict[str, Any] | None = None
    file_uri: str | None = None
    mime_type: str | None = None


def _canonical_bytes(payload: Any) -> bytes:
    """Serialise *payload* the way the rest of the repo canonicalises for hashing."""
    return json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")


def content_hash_for_parts(parts: Iterable[MessagePart]) -> str:
    """Return the ``sha256:`` digest over the canonical A2A message parts.

    This digest is the join key for dedup, diff and lineage (ADR-0142 D1), so
    it must be reproducible by anyone holding the same parts — hence canonical
    JSON with sorted keys, and ``exclude_none`` so an explicitly-null optional
    hashes the same as an omitted one.

    Part **order is significant**: the list is hashed as a list, not a set. Two
    messages made of the same parts in a different order say different things,
    and a digest that collapsed them would let a reordering pass as identical.

    Raises:
        EmptyMessagePartsError: if *parts* is empty. A digest over zero parts is
            well-defined but binds nothing, so a caller whose part extraction
            silently failed would obtain a hash that looks like evidence and
            verifies against any other empty message.
    """
    body = [p.model_dump(exclude_none=True) for p in parts]
    if not body:
        raise EmptyMessagePartsError(
            "an A2A message envelope cannot be content-bound to zero parts; "
            "record the gap instead (ADR-0142 I-3 — capture is fail-open, but a "
            "hash that binds nothing must not be presented as a binding)"
        )
    return f"sha256:{hashlib.sha256(_canonical_bytes(body)).hexdigest()}"


def card_fingerprint(card: Mapping[str, Any]) -> str:
    """Return the ``sha256:`` fingerprint of a signed A2A Agent Card.

    Computed over the card's *claims* — every key except the signature block
    (see ``_UNFINGERPRINTED_CARD_KEYS``) — so the fingerprint identifies which
    card an agent presented, stably across re-signing.
    """
    claims = {k: v for k, v in card.items() if k not in _UNFINGERPRINTED_CARD_KEYS}
    return f"sha256:{hashlib.sha256(_canonical_bytes(claims)).hexdigest()}"


# ── Envelope ──────────────────────────────────────────────────────────────


class A2AMessageEnvelope(BaseModel):
    """One agent→agent A2A message, recorded by reference (NF-101)."""

    model_config = ConfigDict(extra="allow")

    schema_version: str = SCHEMA_VERSION
    msg_id: str
    from_agent: str
    to_agent: str
    a2a_role: A2ARole
    task_id: str
    #: ``sha256:`` over the canonical message parts. The parts are not stored.
    content_hash: str
    #: Always ``None`` in P1. Typed as ``None`` rather than ``str | None`` so
    #: that "no byte capture yet" is enforced by the type checker and by
    #: validation, not merely by a promise in the docstring (I-2).
    blob_ref: None = None
    card_fingerprint: str | None = None
    #: Tri-state on purpose: ``True`` verified, ``False`` verification *failed*,
    #: ``None`` not checked. Defaulting an unchecked card to ``False`` would
    #: manufacture a failed-verification verdict nobody reached (I-3).
    card_verified: bool | None = None
    #: Carried for NF-105 (P3); nothing in P1 reads it as an ordering claim.
    vclock: dict[str, int] | None = None
    sig: str | None = None
    sent_at: str


def envelope_from_parts(
    *,
    msg_id: str,
    from_agent: str,
    to_agent: str,
    a2a_role: A2ARole,
    task_id: str,
    sent_at: str,
    parts: Iterable[MessagePart],
    sender_card: Mapping[str, Any] | None = None,
    card_verified: bool | None = None,
    vclock: dict[str, int] | None = None,
    sig: str | None = None,
) -> A2AMessageEnvelope:
    """Build an envelope from the live message parts, keeping only the digest.

    The parts are consumed here and never reach the returned object (I-2).

    ``sender_card`` is the sender's signed Agent Card; when absent the envelope
    carries no fingerprint and ``card_verified`` stays ``None``.

    Raises:
        EmptyMessagePartsError: propagated from :func:`content_hash_for_parts`.
        CardBindingError: if a ``card_verified`` verdict is supplied without a
            card. A verdict with nothing to point at cannot be re-checked by a
            verifier, so accepting it would let an unfalsifiable claim into the
            record.
    """
    if card_verified is not None and sender_card is None:
        raise CardBindingError(
            "card_verified was asserted without a sender_card; a card verdict "
            "must name the card it is about (ADR-0142 D1)"
        )
    return A2AMessageEnvelope(
        msg_id=msg_id,
        from_agent=from_agent,
        to_agent=to_agent,
        a2a_role=a2a_role,
        task_id=task_id,
        content_hash=content_hash_for_parts(parts),
        card_fingerprint=(
            card_fingerprint(sender_card) if sender_card is not None else None
        ),
        card_verified=card_verified,
        vclock=vclock,
        sig=sig,
        sent_at=sent_at,
    )


# ── Facet ─────────────────────────────────────────────────────────────────


class A2AMessagesFacet(BaseModel):
    """The optional ``facets.a2a_messages`` block (I-1)."""

    model_config = ConfigDict(extra="allow")

    schema_version: str = SCHEMA_VERSION
    a2a_version: str = A2A_VERSION
    messages: list[A2AMessageEnvelope] = Field(default_factory=list)


def build_facet(
    messages: Iterable[A2AMessageEnvelope],
    *,
    a2a_version: str = A2A_VERSION,
) -> A2AMessagesFacet:
    """Assemble the facet, ordered deterministically by ``(sent_at, msg_id)``.

    ``msg_id`` breaks ties so the order is total and the serialised facet — and
    therefore any hash taken over it — is reproducible. This is a serialisation
    order, **not** a causal one; see the module docstring.

    Raises:
        DuplicateMessageIdError: if two envelopes share a ``msg_id``. Silently
            de-duplicating would drop a message from the record, and keeping
            both would leave the facet ambiguous about which one a later
            handoff or provenance edge refers to.
    """
    ordered = sorted(messages, key=lambda m: (m.sent_at, m.msg_id))
    seen: set[str] = set()
    for envelope in ordered:
        if envelope.msg_id in seen:
            raise DuplicateMessageIdError(
                f"duplicate A2A msg_id {envelope.msg_id!r} in one facet"
            )
        seen.add(envelope.msg_id)
    return A2AMessagesFacet(a2a_version=a2a_version, messages=ordered)


def attach_facet(capsule: dict[str, Any], facet: A2AMessagesFacet) -> dict[str, Any]:
    """Attach the A2A message facet to a capsule dict, additively.

    Writes nothing when there are no messages: a single-agent run must be
    byte-identical to one captured before this feature existed (I-1). Returns a
    new dict; the input is not mutated.
    """
    if not facet.messages:
        return capsule
    dumped = facet.model_dump(exclude_none=True)
    for message in dumped["messages"]:
        # `blob_ref: null` is stated, never omitted. "No body was captured" is a
        # claim a reader should see on the record rather than infer from a
        # missing key — the one field where silence could be read as "unknown"
        # when it is in fact a guarantee (I-2).
        message["blob_ref"] = None
    out = dict(capsule)
    facets = dict(out.get("facets") or {})
    facets[FACET_NAME] = dumped
    out["facets"] = facets
    return out


# ── Verification ──────────────────────────────────────────────────────────


def verify_content_binding(
    envelope: A2AMessageEnvelope, parts: Iterable[MessagePart]
) -> bool:
    """Re-verify an envelope's digest against the message parts it claims to bind.

    Returns ``False`` for an envelope carrying no ``content_hash``. An unbound
    envelope is exactly what this check exists to surface; returning ``True``
    would let the messages with nothing to check sail through the binding check.
    """
    if not envelope.content_hash:
        return False
    try:
        return envelope.content_hash == content_hash_for_parts(parts)
    except EmptyMessagePartsError:
        # Nothing to compare against — not a match, and emphatically not a pass.
        return False


def verify_card_binding(
    envelope: A2AMessageEnvelope, card: Mapping[str, Any]
) -> bool:
    """Re-verify an envelope's card fingerprint against a candidate Agent Card.

    Returns ``False`` when the envelope carries no fingerprint, for the same
    reason as :func:`verify_content_binding`. Note this checks *which card*, not
    whether that card's signature is valid — signature verification is the
    caller's, and its outcome belongs in ``card_verified``.
    """
    if not envelope.card_fingerprint:
        return False
    return envelope.card_fingerprint == card_fingerprint(card)


def card_status(envelope: A2AMessageEnvelope) -> CardStatus:
    """Report an envelope's card provenance as ``verified``/``unverified``/``unknown``.

    ``unknown`` is returned whenever no verdict was recorded, or a fingerprint
    is missing so no verdict *could* be about anything. Collapsing that into
    ``unverified`` would report a failure that never happened (I-3).
    """
    if envelope.card_fingerprint is None or envelope.card_verified is None:
        return "unknown"
    return "verified" if envelope.card_verified else "unverified"
