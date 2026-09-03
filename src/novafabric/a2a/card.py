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

"""Portable A2A Agent Card facet — ADR-0149 D1 / NF-171.

The `a2a_messages` facet (ADR-0142, NF-101) binds a *message* to the card an agent
presented, and NF-089 uses the same fingerprint for *delegation containment*. This
module owns the third use the ADR distinguishes: **portability**. The full signed
card travels with the capsule as a content-addressed document, so an A2A-aware
tool that has never seen NovaFabric can read the agent's identity out of the
evidence.

Three things shape the design:

- **One notion of card identity.** ``card_fingerprint`` is imported from
  ``a2a.messages``, not re-derived. NF-089, NF-101 and NF-171 must agree on which
  card an agent presented, and three private implementations of that would be
  three chances to disagree.
- **``signature_ok`` is tri-state, and defaults to "not checked".** A2A 1.0 cards
  are JWS-signed; this repository has no JWS verifier, and inventing one — or
  worse, reporting ``True`` because the card merely *has* a signature block —
  would put a fabricated verdict into evidence. ``None`` with a
  ``signature_status`` naming the reason is the honest record, and it is what
  spec I-3 asks for: a partial object degrades to a recorded gap.
- **Offline re-verification answers the question portability actually raises** —
  *has this card been altered since capture?* :func:`verify_facet` recomputes the
  fingerprint from the stored card. That needs no key material and no network.

Fail-open: no card means no facet, and a capsule without one is byte-identical to
one captured before this feature existed.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from novafabric.a2a.messages import card_fingerprint

SCHEMA_VERSION = "0.1.0"
FACET_NAME = "a2a_card"
DEFAULT_A2A_VERSION = "1.0"

#: Keys whose presence means the card carries a signature block.
_SIGNATURE_KEYS = ("signature", "signatures")

#: Fields the A2A 1.0 card is expected to carry. Absence is recorded, not fatal:
#: a partial card still travels, and the gap is visible (spec I-3).
EXPECTED_CARD_FIELDS = ("name", "provider", "url", "skills", "capabilities",
                        "securitySchemes")

#: Signature verdict when nothing verified the signature.
UNVERIFIED = "unverified: no JWS verifier configured (ADR-0149 NF-171 P1)"
UNSIGNED = "unsigned: the card carries no signature block"


class A2ACardError(ValueError):
    """An A2A card facet could not be built, read, or verified."""


#: A caller-supplied signature verifier. Returns True/False; anything it raises is
#: recorded as a failed verification rather than propagated, so a broken verifier
#: cannot block a capture (I-3).
CardVerifier = Callable[[Mapping[str, Any]], bool]


class A2ACardFacet(BaseModel):
    """The full signed A2A Agent Card, portable and content-addressed."""

    model_config = ConfigDict(extra="allow", frozen=True)

    schema_version: str = SCHEMA_VERSION
    a2a_version: str = DEFAULT_A2A_VERSION
    well_known_url: str | None = None
    #: The card verbatim. Stored whole, because the point is portability: a
    #: consumer must be able to read the agent's identity without NovaFabric.
    card: dict[str, Any]
    card_fingerprint: str
    #: Structural: the card carries a signature block. Knowable without a verifier.
    signed: bool
    #: True/False only when a verifier ran. None means nobody checked — see
    #: ``signature_status``. Never inferred from ``signed``.
    signature_ok: bool | None = None
    signature_status: str = UNVERIFIED
    #: Fields of the expected A2A 1.0 shape that this card does not carry.
    missing_fields: list[str] = Field(default_factory=list)
    #: Sealed capsule root this facet is bound into, once known.
    bound_root: str | None = None
    #: Relative path of the standalone export, when one was written.
    portable_export: str | None = None

    @field_validator("card")
    @classmethod
    def _card_not_empty(cls, v: dict[str, Any]) -> dict[str, Any]:
        if not v:
            raise ValueError("card must not be empty; omit the facet instead (I-3)")
        return v


# ── Construction ──────────────────────────────────────────────────────────


def is_signed(card: Mapping[str, Any]) -> bool:
    """True when the card carries a signature block.

    Structural only. It says a signature is *present*, never that it is valid —
    conflating the two is exactly how a fabricated verdict reaches evidence.
    """
    return any(card.get(k) for k in _SIGNATURE_KEYS)


def build_facet(
    card: Mapping[str, Any],
    *,
    well_known_url: str | None = None,
    a2a_version: str = DEFAULT_A2A_VERSION,
    verifier: CardVerifier | None = None,
    bound_root: str | None = None,
    portable_export: str | None = None,
) -> A2ACardFacet:
    """Build the ``a2a_card`` facet from a card document.

    *verifier* is optional by design: with none supplied ``signature_ok`` stays
    ``None`` and ``signature_status`` says so. A verifier that raises is recorded
    as a failed verification rather than propagated — a broken verifier must not
    block a capture (I-3).
    """
    if not card:
        raise A2ACardError("card must not be empty; omit the facet instead")

    signed = is_signed(card)
    ok: bool | None = None
    if not signed:
        status = UNSIGNED
    elif verifier is None:
        status = UNVERIFIED
    else:
        try:
            ok = bool(verifier(card))
            status = "verified" if ok else "signature verification failed"
        except Exception as exc:  # noqa: BLE001 - recorded, never raised
            ok = False
            status = f"signature verification error: {exc}"

    missing = [f for f in EXPECTED_CARD_FIELDS if f not in card]

    return A2ACardFacet(
        a2a_version=a2a_version,
        well_known_url=well_known_url,
        card=dict(card),
        card_fingerprint=card_fingerprint(card),
        signed=signed,
        signature_ok=ok,
        signature_status=status,
        missing_fields=missing,
        bound_root=bound_root,
        portable_export=portable_export,
    )


# ── Capsule facet ─────────────────────────────────────────────────────────


def attach_facet(
    capsule: dict[str, Any], facet: A2ACardFacet | None
) -> dict[str, Any]:
    """Attach the card facet additively; returns a new dict, input untouched."""
    if facet is None:
        return capsule
    out = dict(capsule)
    facets = dict(out.get("facets") or {})
    facets[FACET_NAME] = facet.model_dump(exclude_none=True)
    out["facets"] = facets
    return out


def facet_from_capsule(capsule: dict[str, Any]) -> A2ACardFacet | None:
    """Read the card facet back out of a capsule dict, or None if absent."""
    facets = capsule.get("facets")
    if not isinstance(facets, dict):
        return None
    block = facets.get(FACET_NAME)
    if not isinstance(block, dict):
        return None
    try:
        return A2ACardFacet.model_validate(block)
    except ValueError as exc:
        raise A2ACardError(f"capsule holds an invalid a2a_card facet: {exc}") from exc


# ── Offline re-verification ───────────────────────────────────────────────


class CardVerification(BaseModel):
    """Result of re-checking a stored card against its recorded fingerprint."""

    model_config = ConfigDict(extra="allow", frozen=True)

    recorded_fingerprint: str
    observed_fingerprint: str
    fingerprint_matches: bool
    signed: bool
    signature_ok: bool | None
    signature_status: str


def verify_facet(facet: A2ACardFacet) -> CardVerification:
    """Recompute the fingerprint from the stored card and report agreement.

    This is the offline re-verification NF-171 requires, and it answers the
    question portability actually raises — *has the card been altered since
    capture?* — with no key material and no network.

    The facet is never modified, not even on a mismatch. Rewriting the recorded
    fingerprint to the observed one would redefine which card was presented and
    turn a detected alteration into a silently accepted new identity.
    """
    observed = card_fingerprint(facet.card)
    return CardVerification(
        recorded_fingerprint=facet.card_fingerprint,
        observed_fingerprint=observed,
        fingerprint_matches=observed == facet.card_fingerprint,
        signed=facet.signed,
        signature_ok=facet.signature_ok,
        signature_status=facet.signature_status,
    )


# ── Portable export ───────────────────────────────────────────────────────


def export_filename(fingerprint: str) -> str:
    """Content-addressed export name, e.g. ``a2a-card-9f22ab1c.json``."""
    short = fingerprint.split(":", 1)[-1][:8]
    return f"a2a-card-{short}.json"


def write_portable_export(facet: A2ACardFacet, outputs_dir: Path) -> Path:
    """Write the card as a standalone document under *outputs_dir*.

    The file holds the card **verbatim** — not the facet — so any A2A-aware tool
    can read it without knowing anything about NovaFabric's schema. That is the
    whole point of NF-171.
    """
    outputs_dir.mkdir(parents=True, exist_ok=True)
    path = outputs_dir / export_filename(facet.card_fingerprint)
    path.write_text(
        json.dumps(facet.card, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return path
