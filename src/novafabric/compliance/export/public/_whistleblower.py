"""Journalism/whistleblower tamper-evident, source-protecting attestation (ADR-0169 D1 / NF-375).

A pure exporter that assembles a **source-protecting** statement over an already-sealed bundle so a
journalist or recipient can prove the material is authentic **without** learning who supplied it:

* ``content_digest`` — the digest of the sealed bundle the attestation is about,
* ``authenticity_attestation`` — a reference to the bundle's **existing** Evidence-Bundle Ed25519
  signature. This slice **never signs** (signing lives on the seal path, off-limits here); it binds
  the already-produced signature by reference,
* ``anonymity_set_ref`` — an optional reference to an anonymity set / group so authenticity is
  provable without singling out an individual source.

The hard invariant (I-5, and ADR-0009 / ADR-0021 §4): this attestation **MUST NOT** capture or embed
source identity, contact, or routing metadata. The exporter therefore **rejects** any supplied field
whose name matches a source-identifying / contact / routing shape — a leaked ``submitter_email`` or
``ip_address`` is a hard error, never silently carried. The model itself has no field that could
hold such data, so the shape is enforced both structurally and by the validator.
"""
from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel

#: Substrings that mark a field as source-identifying / contact / routing (lowercased match).
#: Curated so the three legitimate fields never match; anything matching here is rejected outright.
SOURCE_IDENTIFYING_PATTERNS: tuple[str, ...] = (
    "source",
    "submitter",
    "sender",
    "reporter",
    "informant",
    "whistleblower_name",
    "identity",
    "contact",
    "email",
    "phone",
    "mobile",
    "ip_addr",
    "ip_address",
    "routing",
    "route",
    "address",
    "geoloc",
    "location",
    "username",
    "user_id",
    "handle",
    "personal",
    "ssn",
    "passport",
    "real_name",
    "full_name",
    "legal_name",
    "first_name",
    "last_name",
)

_REQUIRED: tuple[str, ...] = ("content_digest", "authenticity_attestation")


class WhistleblowerAttestation(BaseModel):
    content_digest: str  # digest of the sealed bundle the attestation is about
    # ref to the bundle's existing Ed25519 signature — this slice binds it, never re-signs.
    authenticity_attestation: str
    anonymity_set_ref: str | None = None  # optional anonymity-set/group ref — proves without naming


def source_identifying_fields(fields: Mapping[str, object]) -> list[str]:
    """Return the supplied field names that match a source-identifying / contact / routing shape."""
    return [
        name
        for name in fields
        if any(pat in name.lower() for pat in SOURCE_IDENTIFYING_PATTERNS)
    ]


def build_whistleblower_attestation(
    document: Mapping[str, object],
) -> WhistleblowerAttestation:
    """Assemble a source-protecting whistleblower attestation from ``document``.

    Rejects (``ValueError``) any supplied field matching a source-identity / contact / routing
    shape — a leaked identifier is a hard error, never silently carried. Requires ``content_digest``
    and ``authenticity_attestation``; ``anonymity_set_ref`` is optional.
    """
    leaked = source_identifying_fields(document)
    if leaked:
        raise ValueError(
            "refusing to build a source-protecting attestation: source-identifying field(s) "
            f"present and must never be embedded: {', '.join(sorted(leaked))}"
        )
    missing = [name for name in _REQUIRED if not document.get(name)]
    if missing:
        raise ValueError(f"missing required field(s): {', '.join(missing)}")

    anon = document.get("anonymity_set_ref")
    return WhistleblowerAttestation(
        content_digest=str(document["content_digest"]),
        authenticity_attestation=str(document["authenticity_attestation"]),
        anonymity_set_ref=str(anon) if anon is not None else None,
    )
