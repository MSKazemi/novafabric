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

"""Crypto re-seal events + LTV renewal chain — ADR-0165 D2/D3 P3 (NF-333, NF-334).

Record-only half of P3. A capsule sealed under Ed25519 in 2026 has to still be
provable in 2040, after Ed25519 is disallowed and after the hash its RFC 3161
timestamp used has weakened. Two things keep it provable, and this module
records *that* each happened — it performs neither:

- **NF-333 ``crypto_migration``** — an append-only list of re-seal events
  (``ed25519 → ml-dsa-65``). Each event carries ``from_alg``, ``to_alg``,
  ``resealed_at``, ``upgrade_ref`` (an **opaque** reference to the NF-192
  ``upgrade-signature`` operation that did the signing), the **required
  literal** ``original_sig_preserved: true``, and ``renewal_timestamp_ref``
  (the fresh timestamp over the re-seal). A re-seal that dropped the original
  signature is not a migration, it is a replacement, and it is rejected (I-1).
  The re-seal verifier ranks signature algorithms by
  :data:`SIGNATURE_STRENGTH`: a step down (PQC → classic, or within a tier)
  is a finding, and an unranked algorithm is a finding (fail closed).
- **NF-334 ``ltv_renewal_chain``** — an ordered list of RFC 4998
  archive-timestamp renewals. Each renewal carries ``renewal_type``
  (``timestamp_renewal`` | ``hash_tree_renewal``), ``covered_digest`` (what the
  new timestamp covers — the prior evidence, including prior timestamps),
  ``new_timestamp_ref``, ``new_hash_alg`` and ``renewed_before`` (the sunset it
  beat), plus a derived ``parent`` link and optional ``renewed_at`` /
  ``timestamp_expires_at``.

**What the LTV verifier can and cannot decide offline.** It holds digests and
references, never tokens, so it checks the *record's* internal consistency:

- *covers the previous renewal* — every renewal after the first names the
  previous renewal's ``new_timestamp_ref`` as its ``parent``; a
  ``timestamp_renewal`` (RFC 4998 §5.2 — re-timestamp the previous archive
  timestamp) must have ``covered_digest == parent``; a ``hash_tree_renewal``
  (§5.3 — re-hash data plus the whole prior timestamp sequence under a new
  algorithm) must have a ``covered_digest`` computed under its own
  ``new_hash_alg`` and never re-cover an already-covered state;
- *in time* — ``renewed_before`` never moves backwards, a renewal's deadline
  is no later than the prior timestamp's ``timestamp_expires_at`` where that
  was recorded, and a recorded ``renewed_at`` is before both;
- *never weaker* — hash algorithms follow :data:`HASH_STRENGTH`; a step down is
  a finding, an algorithm missing from that table is a finding (fail closed),
  and a ``timestamp_renewal`` may not change the hash (only a hash-tree
  renewal can, per RFC 4998).

It does **not** recompute a hash-tree digest (it has no bytes), dereference a
timestamp token, or check a TSA signature: that is the NF-339 re-verification
receipt (P5, future design). The first renewal's ``covered_digest`` covers the
seal-time evidence, whose timestamp this layer does not hold, so it is
recorded but not cross-checked.

Record-only (I-4) and secret-free (I-2): no key generation, no TSA call, no
ML-DSA code (no such library is a dependency). References are identifiers,
bounded in length, and refused when they embed credentials or PEM key
material. Verdicts are returned, never persisted into the facet.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import UTC, date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from novafabric.preservation.anchor import (
    MAX_REF_LENGTH,
    PreservationError,
    PreservationFacet,
    _validate_ref,
    append_provenance_event,
    provenance_event,
)

__all__ = [
    "CRYPTO_MIGRATION_FIELD",
    "HASH_STRENGTH",
    "LTV_CHAIN_FIELD",
    "LTV_RENEWAL_EVENT",
    "MAX_RECORDS",
    "RESEAL_EVENT",
    "SIGNATURE_STRENGTH",
    "BrokenLtvChainError",
    "BrokenResealRecordError",
    "CryptoMigrationEvent",
    "LtvFinding",
    "LtvFindingCode",
    "LtvRenewal",
    "LtvVerification",
    "OriginalSignatureDroppedError",
    "P3RewriteError",
    "RenewalType",
    "ResealFinding",
    "ResealFindingCode",
    "ResealVerification",
    "append_crypto_migration",
    "append_ltv_renewal",
    "crypto_migrations_from_facet",
    "ltv_chain_from_facet",
    "plan_ltv_renewal",
    "plan_reseal",
    "verify_crypto_migrations",
    "verify_ltv_chain",
    "verify_p3_append_only",
]

#: Keys inside ``facets.preservation`` (``extra="allow"`` — no schema change).
CRYPTO_MIGRATION_FIELD = "crypto_migration"
LTV_CHAIN_FIELD = "ltv_renewal_chain"

#: PREMIS 3 eventType (id.loc.gov vocabulary) for a re-seal.
RESEAL_EVENT = "digital signature generation"
#: Free-text event for an LTV renewal; PREMIS 3 has no archive-timestamp term,
#: and the anchor already accepts free-text events (``ingested-to-worm``).
LTV_RENEWAL_EVENT = "timestamp renewal"

#: Upper bound on either list; a hostile archive must not turn a linear walk
#: into an unbounded one. Far beyond any real decades-long history.
MAX_RECORDS = 10_000

#: Hash algorithm → collision-resistance bits, the explicit strength order the
#: no-downgrade rule uses. SHA-2 and SHA-3 of equal output length rank equal:
#: ``sha256 → sha3-256`` is a lateral move to a different construction (the
#: spec's own example), not a downgrade. Anything absent — md5, sha1, a typo,
#: an algorithm invented after this table — is *unknown*, and unknown is a
#: finding, not a pass: a verifier that cannot rank an algorithm cannot say
#: the chain did not weaken.
HASH_STRENGTH: dict[str, int] = {
    "sha224": 112,
    "sha3-224": 112,
    "sha256": 128,
    "sha3-256": 128,
    "sha384": 192,
    "sha3-384": 192,
    "sha512": 256,
    "sha3-512": 256,
}

#: Hex length of a digest for each known algorithm.
_HEX_LENGTH: dict[str, int] = {alg: bits // 2 for alg, bits in HASH_STRENGTH.items()}

#: Signature algorithm → ``(tier, level)``, the explicit strength order the
#: re-seal *never weaker* rule uses (compared as tuples, so any post-quantum
#: scheme outranks any classic one — a PQC re-seal exists to survive a
#: cryptographically relevant quantum computer, which breaks every classic
#: scheme regardless of key size).
#:
#: * tier ``0`` — classic; level = classical security bits per NIST SP 800-57
#:   Pt 1 Rev 5 Table 2 (RSA-2048 → 112, RSA-3072 → 128; RSA-4096 is ranked at
#:   the 128 floor it is guaranteed to meet, not an interpolated figure) and
#:   curve order / 2 for EdDSA and ECDSA (Ed25519/P-256 → 128, P-384 → 192,
#:   Ed448 → 224, P-521 → 256). PSS and PKCS#1 v1.5 padding rank equal: the
#:   rank is key strength, not padding hygiene.
#: * tier ``1`` — post-quantum; level = NIST PQC security category (FIPS 204
#:   §4 for ML-DSA: 44 → 2, 65 → 3, 87 → 5; FIPS 205 §11 for SLH-DSA:
#:   128s/f → 1, 192s/f → 3, 256s/f → 5). Categories are the only ranking
#:   NIST publishes for these schemes; equal categories are a lateral move.
#:
#: Anything absent is *unknown*, and unknown is a finding (fail closed).
SIGNATURE_STRENGTH: dict[str, tuple[int, int]] = {
    "rsa-pss-2048": (0, 112),
    "rsa-pkcs1-2048": (0, 112),
    "rsa-pss-3072": (0, 128),
    "rsa-pkcs1-3072": (0, 128),
    "rsa-pss-4096": (0, 128),
    "rsa-pkcs1-4096": (0, 128),
    "ed25519": (0, 128),
    "ecdsa-p256": (0, 128),
    "ecdsa-p384": (0, 192),
    "ed448": (0, 224),
    "ecdsa-p521": (0, 256),
    "slh-dsa-sha2-128s": (1, 1),
    "slh-dsa-sha2-128f": (1, 1),
    "slh-dsa-shake-128s": (1, 1),
    "slh-dsa-shake-128f": (1, 1),
    "ml-dsa-44": (1, 2),
    "ml-dsa-65": (1, 3),
    "slh-dsa-sha2-192s": (1, 3),
    "slh-dsa-sha2-192f": (1, 3),
    "slh-dsa-shake-192s": (1, 3),
    "slh-dsa-shake-192f": (1, 3),
    "ml-dsa-87": (1, 5),
    "slh-dsa-sha2-256s": (1, 5),
    "slh-dsa-sha2-256f": (1, 5),
    "slh-dsa-shake-256s": (1, 5),
    "slh-dsa-shake-256f": (1, 5),
}
_PQC_TIER = 1

#: Algorithm identifier syntax (hash or signature): lower-case, bounded.
_ALG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}\Z")

#: ``<alg>:<hex>`` — generic on purpose: a hash-tree renewal under sha3-256
#: yields a ``sha3-256:`` digest. Whether the algorithm is known and the length
#: right is judged by the verifier, which reports rather than refuses.
_ALG_DIGEST_RE = re.compile(r"^([a-z0-9][a-z0-9-]{0,15}):([0-9a-f]{40,128})\Z")

#: Opaque reference (e.g. ``NF-192:upgrade-signature#op-1``): a token, not a
#: document. No ``@``: ``user:pass@host`` must not pass as a token. URIs and
#: ``sha256:`` digests also pass via :func:`_validate_ref`.
_OPAQUE_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/#+=~\-]{0,255}\Z")

_URI_USERINFO_RE = re.compile(r"^[a-z][a-z0-9+.\-]*://[^/?#]*@", re.IGNORECASE)
_SECRET_MARKER_RE = re.compile(r"-----BEGIN|PRIVATE KEY", re.IGNORECASE)

RenewalType = Literal["timestamp_renewal", "hash_tree_renewal"]

ResealFindingCode = Literal[
    "no_alg_change",
    "alg_discontinuity",
    "pqc_to_classic_downgrade",
    "signature_alg_downgrade",
    "unknown_signature_alg",
    "time_not_monotonic",
    "renewal_timestamp_reused",
    "too_many_records",
]

LtvFindingCode = Literal[
    "first_renewal_has_parent",
    "missing_parent",
    "covered_digest_mismatch",
    "covered_alg_mismatch",
    "covered_digest_reused",
    "timestamp_ref_reused",
    "unknown_hash_alg",
    "digest_length_mismatch",
    "hash_alg_downgrade",
    "hash_alg_changed_by_timestamp_renewal",
    "renewed_before_not_monotonic",
    "renewed_after_prior_expiry",
    "renewed_at_not_before_deadline",
    "too_many_records",
]

_COVER_CODES = frozenset(
    {
        "first_renewal_has_parent",
        "missing_parent",
        "covered_digest_mismatch",
        "covered_alg_mismatch",
        "covered_digest_reused",
        "timestamp_ref_reused",
        "too_many_records",
    }
)
_ALG_CODES = frozenset(
    {
        "unknown_hash_alg",
        "digest_length_mismatch",
        "hash_alg_downgrade",
        "hash_alg_changed_by_timestamp_renewal",
        "too_many_records",
    }
)
_TIME_CODES = frozenset(
    {
        "renewed_before_not_monotonic",
        "renewed_after_prior_expiry",
        "renewed_at_not_before_deadline",
        "too_many_records",
    }
)


# ── Errors ────────────────────────────────────────────────────────────────


class OriginalSignatureDroppedError(PreservationError):
    """A re-seal record does not assert ``original_sig_preserved: true`` (I-1).

    Distinct from a malformed record: the record is well-formed and says the
    original signature was dropped or overwritten, which is a refusal, not a
    typo. Callers map it to "broken" (exit 1), not "bad input" (exit 2).
    """


class BrokenResealRecordError(PreservationError):
    """Raised when re-seal events fail verification where valid ones are required."""

    def __init__(self, message: str, verification: ResealVerification) -> None:
        super().__init__(message)
        self.verification = verification


class BrokenLtvChainError(PreservationError):
    """Raised when an LTV chain fails verification where a valid one is required."""

    def __init__(self, message: str, verification: LtvVerification) -> None:
        super().__init__(message)
        self.verification = verification


class P3RewriteError(PreservationError):
    """A newer facet did not merely append to an older one's P3 records (I-1)."""


# ── Field validation helpers ──────────────────────────────────────────────


def _parse_instant(value: str, *, field: str) -> datetime:
    """Parse a tz-aware RFC 3339 instant; naive times cannot be ordered."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an RFC 3339 timestamp, got {value!r}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must carry a timezone (e.g. 'Z'), got {value!r}")
    return parsed


def _parse_deadline(value: str, *, field: str) -> datetime:
    """Parse a ``YYYY-MM-DD`` date (midnight UTC) or an RFC 3339 instant."""
    if isinstance(value, str) and len(value) == 10:
        try:
            day = date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"{field} must be YYYY-MM-DD or RFC 3339, got {value!r}") from exc
        return datetime(day.year, day.month, day.day, tzinfo=UTC)
    return _parse_instant(value, field=field)


def _check_alg(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not _ALG_RE.match(value):
        raise ValueError(f"{field} must be a lower-case algorithm identifier, e.g. 'ml-dsa-65'")
    return value


def _check_alg_digest(value: object, *, field: str) -> str:
    """``<alg>:<hex>`` content digest; bytes and oversize input refused (I-2)."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        raise ValueError(f"{field} must be a digest string, not raw bytes (ADR-0165 I-2)")
    if not isinstance(value, str) or not _ALG_DIGEST_RE.match(value):
        raise ValueError(f"{field} must be a content digest '<alg>:<hex>', e.g. 'sha256:<64 hex>'")
    return value


def _check_opaque_ref(value: object, *, field: str) -> str:
    """A digest, URI, or bounded opaque token — never a credential or key."""
    if isinstance(value, str) and _SECRET_MARKER_RE.search(value):
        raise ValueError(f"{field} carries key material; a reference never does (ADR-0165 I-2)")
    if isinstance(value, str) and _URI_USERINFO_RE.match(value):
        raise ValueError(
            f"{field} URI embeds credentials (user[:password]@host); a reference "
            "must never carry a secret (ADR-0165 I-2, ADR-0009)"
        )
    if isinstance(value, str) and len(value) <= MAX_REF_LENGTH and _OPAQUE_REF_RE.match(value):
        return value
    try:
        return _validate_ref(value, field=field)
    except PreservationError as exc:
        raise ValueError(str(exc)) from exc


def _digest_alg(digest: str) -> str:
    return digest.split(":", 1)[0]


# ── NF-333 model + verification ───────────────────────────────────────────


class CryptoMigrationEvent(BaseModel):
    """One crypto re-seal event (NF-333, spec §3 req. 8).

    ``original_sig_preserved`` is ``Literal[True]`` with no default: the record
    must *say* the original survived. There is no representable "false".
    """

    model_config = ConfigDict(extra="allow")

    from_alg: str
    to_alg: str
    resealed_at: str
    #: Opaque reference to the NF-192 ``upgrade-signature`` operation.
    upgrade_ref: str
    original_sig_preserved: Literal[True]
    #: Reference to the fresh timestamp over the re-seal.
    renewal_timestamp_ref: str

    @field_validator("from_alg", "to_alg", mode="before")
    @classmethod
    def _v_alg(cls, v: object) -> str:
        return _check_alg(v, field="alg")

    @field_validator("resealed_at")
    @classmethod
    def _v_time(cls, v: str) -> str:
        _parse_instant(v, field="resealed_at")
        return v

    @field_validator("upgrade_ref", "renewal_timestamp_ref", mode="before")
    @classmethod
    def _v_ref(cls, v: object) -> str:
        return _check_opaque_ref(v, field="reference")


class ResealFinding(BaseModel):
    """One fault in the re-seal record."""

    model_config = ConfigDict(frozen=True)

    index: int
    code: ResealFindingCode
    message: str


class ResealVerification(BaseModel):
    """Outcome of checking the ``crypto_migration`` list. Derived from findings."""

    model_config = ConfigDict(frozen=True)

    event_count: int
    original_sig_preserved: bool
    scheme_migrations: list[str] = Field(default_factory=list)
    findings: list[ResealFinding] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when the original signature is preserved and nothing was found."""
        return self.original_sig_preserved and not self.findings


def _raw_list(facet: PreservationFacet, key: str) -> list[Any]:
    raw = (facet.model_extra or {}).get(key)
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise PreservationError(f"{key} must be a list, got {type(raw).__name__}")
    if len(raw) > MAX_RECORDS:
        raise PreservationError(f"{key} has {len(raw)} entries, over the {MAX_RECORDS} limit")
    return raw


def _first_error(exc: ValidationError) -> str:
    err = exc.errors(include_url=False)[0]
    loc = ".".join(str(p) for p in err["loc"])
    return f"{loc}: {err['msg']}" if loc else str(err["msg"])


def crypto_migrations_from_facet(facet: PreservationFacet) -> list[CryptoMigrationEvent]:
    """Return the facet's re-seal events in recorded order (never sorted).

    Raises:
        OriginalSignatureDroppedError: an event does not carry the literal
            ``original_sig_preserved: true`` (checked before shape, so the
            I-1 violation is never masked by a lesser error).
        PreservationError: the list is oversized or an event is malformed.
    """
    events: list[CryptoMigrationEvent] = []
    for index, item in enumerate(_raw_list(facet, CRYPTO_MIGRATION_FIELD)):
        if isinstance(item, CryptoMigrationEvent):
            events.append(item)
            continue
        if not isinstance(item, dict) or item.get("original_sig_preserved") is not True:
            raise OriginalSignatureDroppedError(
                f"{CRYPTO_MIGRATION_FIELD}[{index}] does not assert "
                "original_sig_preserved: true; a re-seal that dropped or "
                "overwrote the original signature is rejected (ADR-0165 I-1)"
            )
        try:
            events.append(CryptoMigrationEvent.model_validate(item))
        except ValidationError as exc:
            raise PreservationError(
                f"{CRYPTO_MIGRATION_FIELD}[{index}] is malformed: {_first_error(exc)}"
            ) from exc
    return events


def _sig_family(alg: str) -> str | None:
    rank = SIGNATURE_STRENGTH.get(alg)
    if rank is None:
        return None
    return "pqc" if rank[0] == _PQC_TIER else "classic"


def _sig_downgrades(from_alg: str, to_alg: str) -> bool:
    """True when both algorithms are ranked and ``to_alg`` ranks strictly lower."""
    before = SIGNATURE_STRENGTH.get(from_alg)
    after = SIGNATURE_STRENGTH.get(to_alg)
    return before is not None and after is not None and after < before


def verify_crypto_migrations(events: Sequence[CryptoMigrationEvent]) -> ResealVerification:
    """Check the re-seal record offline; never raises on a record fault.

    Pure: no IO, no clock. ``original_sig_preserved`` is re-checked here even
    though the model enforces it, so a model built with ``model_construct``
    cannot slip past.
    """
    if len(events) > MAX_RECORDS:
        finding = ResealFinding(
            index=MAX_RECORDS,
            code="too_many_records",
            message=f"more than {MAX_RECORDS} re-seal events; refusing to walk them",
        )
        return ResealVerification(
            event_count=len(events), original_sig_preserved=False, findings=[finding]
        )
    findings: list[ResealFinding] = []
    seen_refs: set[str] = set()
    preserved = all(e.original_sig_preserved is True for e in events)
    for i, event in enumerate(events):
        for alg in (event.from_alg, event.to_alg):
            if _sig_family(alg) is None:
                findings.append(
                    ResealFinding(
                        index=i,
                        code="unknown_signature_alg",
                        message=f"signature algorithm {alg!r} is not in the known "
                        "SIGNATURE_STRENGTH table; the re-seal cannot be ranked",
                    )
                )
        if event.from_alg == event.to_alg:
            findings.append(
                ResealFinding(
                    index=i,
                    code="no_alg_change",
                    message=f"re-seal {event.from_alg} → {event.to_alg} changes nothing",
                )
            )
        if _sig_family(event.from_alg) == "pqc" and _sig_family(event.to_alg) == "classic":
            findings.append(
                ResealFinding(
                    index=i,
                    code="pqc_to_classic_downgrade",
                    message=f"{event.from_alg} → {event.to_alg} moves from a "
                    "post-quantum scheme back to a classic one",
                )
            )
        elif _sig_downgrades(event.from_alg, event.to_alg):
            findings.append(
                ResealFinding(
                    index=i,
                    code="signature_alg_downgrade",
                    message=f"{event.from_alg} → {event.to_alg} lowers signature "
                    "strength under SIGNATURE_STRENGTH; a re-seal is never weaker",
                )
            )
        if event.renewal_timestamp_ref in seen_refs:
            findings.append(
                ResealFinding(
                    index=i,
                    code="renewal_timestamp_reused",
                    message="renewal_timestamp_ref was already used by an earlier "
                    "re-seal; each re-seal needs a fresh timestamp",
                )
            )
        seen_refs.add(event.renewal_timestamp_ref)
        if i > 0:
            prev = events[i - 1]
            if event.from_alg != prev.to_alg:
                findings.append(
                    ResealFinding(
                        index=i,
                        code="alg_discontinuity",
                        message=f"re-seal starts from {event.from_alg} but the "
                        f"previous one ended at {prev.to_alg}",
                    )
                )
            if _parse_instant(event.resealed_at, field="resealed_at") < _parse_instant(
                prev.resealed_at, field="resealed_at"
            ):
                findings.append(
                    ResealFinding(
                        index=i,
                        code="time_not_monotonic",
                        message=f"resealed_at {event.resealed_at} is earlier than "
                        f"the previous re-seal's {prev.resealed_at}",
                    )
                )
    return ResealVerification(
        event_count=len(events),
        original_sig_preserved=preserved,
        scheme_migrations=[f"{e.from_alg}→{e.to_alg}" for e in events],
        findings=findings,
    )


def plan_reseal(
    facet: PreservationFacet,
    *,
    to_alg: str,
    upgrade_ref: str,
    renewal_timestamp_ref: str,
    resealed_at: str,
    from_alg: str | None = None,
) -> CryptoMigrationEvent:
    """Build the next re-seal event; ``from_alg`` defaults to the last ``to_alg``.

    ``original_sig_preserved`` is always ``True`` here — a caller that knows
    the original was dropped must not record the event at all.

    Raises:
        PreservationError: first event without ``from_alg``, or a ``from_alg``
            contradicting the record; or the stored record is malformed.
        ValidationError: a field is malformed.
    """
    events = crypto_migrations_from_facet(facet)
    last = events[-1] if events else None
    if last is None and from_alg is None:
        raise PreservationError(
            "the first re-seal needs from_alg — the algorithm of the original seal"
        )
    if last is not None and from_alg is not None and from_alg != last.to_alg:
        raise PreservationError(
            f"from_alg {from_alg!r} contradicts the record, whose last re-seal "
            f"ended at {last.to_alg!r}"
        )
    resolved = last.to_alg if last is not None else from_alg
    assert resolved is not None  # narrowed by the first-event check above
    return CryptoMigrationEvent(
        from_alg=resolved,
        to_alg=to_alg,
        resealed_at=resealed_at,
        upgrade_ref=upgrade_ref,
        original_sig_preserved=True,
        renewal_timestamp_ref=renewal_timestamp_ref,
    )


def append_crypto_migration(
    facet: PreservationFacet,
    event: CryptoMigrationEvent,
    *,
    record_event: bool = True,
    agent_ref: str | None = None,
) -> PreservationFacet:
    """Append one re-seal event, returning a **new** facet (I-1).

    Refuses to extend a record that already fails verification, or to append
    an event that would make it fail. With ``record_event``, a PREMIS
    ``digital signature generation`` event is appended to
    ``provenance_events`` (``agent_ref`` optional — the opaque ``upgrade_ref``
    is not always a PREMIS agent identifier).

    Raises:
        BrokenResealRecordError: the existing or resulting record fails.
    """
    events = crypto_migrations_from_facet(facet)
    before = verify_crypto_migrations(events)
    if not before.ok:
        raise BrokenResealRecordError(
            "refusing to append to a crypto_migration record that already fails verification",
            before,
        )
    after = verify_crypto_migrations([*events, event])
    if not after.ok:
        raise BrokenResealRecordError(
            "refusing to append a re-seal that would break the record: "
            + "; ".join(f.code for f in after.findings),
            after,
        )
    serialized = [e.model_dump(mode="json") for e in (*events, event)]
    out = facet.model_copy(update={CRYPTO_MIGRATION_FIELD: serialized})
    if record_event:
        out = append_provenance_event(
            out, provenance_event(RESEAL_EVENT, event.resealed_at, agent_ref=agent_ref)
        )
    return out


# ── NF-334 model + verification ───────────────────────────────────────────


class LtvRenewal(BaseModel):
    """One RFC 4998 archive-timestamp renewal (NF-334, spec §3 req. 9).

    ``parent`` is required-but-nullable, as in the format-migration chain: the
    previous renewal's ``new_timestamp_ref``, ``null`` for the first renewal
    (whose prior timestamp is the seal-time one this layer does not hold).
    ``new_timestamp_ref`` is a content digest of the renewed token because it
    is the link the next renewal covers — a locator could be swapped
    underneath the chain.
    """

    model_config = ConfigDict(extra="allow")

    renewal_type: RenewalType
    covered_digest: str
    new_timestamp_ref: str
    new_hash_alg: str
    renewed_before: str
    parent: str | None
    renewed_at: str | None = None
    #: When the renewed timestamp itself stops being trustworthy (TSA
    #: certificate expiry / algorithm sunset), if known.
    timestamp_expires_at: str | None = None

    @field_validator("covered_digest", "new_timestamp_ref", mode="before")
    @classmethod
    def _v_digest(cls, v: object) -> str:
        return _check_alg_digest(v, field="digest")

    @field_validator("parent", mode="before")
    @classmethod
    def _v_parent(cls, v: object) -> str | None:
        return None if v is None else _check_alg_digest(v, field="parent")

    @field_validator("new_hash_alg", mode="before")
    @classmethod
    def _v_alg(cls, v: object) -> str:
        return _check_alg(v, field="new_hash_alg")

    @field_validator("renewed_before", "timestamp_expires_at")
    @classmethod
    def _v_deadline(cls, v: str | None) -> str | None:
        if v is not None:
            _parse_deadline(v, field="deadline")
        return v

    @field_validator("renewed_at")
    @classmethod
    def _v_renewed_at(cls, v: str | None) -> str | None:
        if v is not None:
            _parse_instant(v, field="renewed_at")
        return v


class LtvFinding(BaseModel):
    """One fault found while walking the LTV chain."""

    model_config = ConfigDict(frozen=True)

    renewal_index: int
    code: LtvFindingCode
    message: str


class LtvVerification(BaseModel):
    """Outcome of an offline LTV walk; every boolean is derived from findings."""

    model_config = ConfigDict(frozen=True)

    renewal_count: int
    covers_previous: bool
    hash_algs_ok: bool
    renewed_in_time: bool
    findings: list[LtvFinding] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True only when coverage, algorithm strength and timing all hold."""
        return self.covers_previous and self.hash_algs_ok and self.renewed_in_time


def ltv_chain_from_facet(facet: PreservationFacet) -> list[LtvRenewal]:
    """Return the LTV chain in recorded order (never sorted — order is evidence).

    Raises:
        PreservationError: oversized list or a malformed renewal.
    """
    renewals: list[LtvRenewal] = []
    for index, item in enumerate(_raw_list(facet, LTV_CHAIN_FIELD)):
        try:
            renewals.append(
                item if isinstance(item, LtvRenewal) else LtvRenewal.model_validate(item)
            )
        except ValidationError as exc:
            raise PreservationError(
                f"{LTV_CHAIN_FIELD}[{index}] is malformed: {_first_error(exc)}"
            ) from exc
    return renewals


def _alg_findings(i: int, r: LtvRenewal, prev: LtvRenewal | None) -> list[LtvFinding]:
    out: list[LtvFinding] = []
    known = r.new_hash_alg in HASH_STRENGTH
    if not known:
        out.append(
            LtvFinding(
                renewal_index=i,
                code="unknown_hash_alg",
                message=f"new_hash_alg {r.new_hash_alg!r} is not in the strength "
                "table; the chain cannot be shown not to weaken",
            )
        )
    for field, digest in (
        ("covered_digest", r.covered_digest),
        ("new_timestamp_ref", r.new_timestamp_ref),
    ):
        alg = _digest_alg(digest)
        if alg not in _HEX_LENGTH:
            out.append(
                LtvFinding(
                    renewal_index=i,
                    code="unknown_hash_alg",
                    message=f"{field} uses unknown hash algorithm {alg!r}",
                )
            )
        elif len(digest) - len(alg) - 1 != _HEX_LENGTH[alg]:
            out.append(
                LtvFinding(
                    renewal_index=i,
                    code="digest_length_mismatch",
                    message=f"{field} is not a {_HEX_LENGTH[alg]}-hex-char {alg} digest",
                )
            )
    if prev is not None and known and prev.new_hash_alg in HASH_STRENGTH:
        if HASH_STRENGTH[r.new_hash_alg] < HASH_STRENGTH[prev.new_hash_alg]:
            out.append(
                LtvFinding(
                    renewal_index=i,
                    code="hash_alg_downgrade",
                    message=f"{prev.new_hash_alg} → {r.new_hash_alg} weakens the chain",
                )
            )
    if (
        prev is not None
        and r.renewal_type == "timestamp_renewal"
        and r.new_hash_alg != prev.new_hash_alg
    ):
        out.append(
            LtvFinding(
                renewal_index=i,
                code="hash_alg_changed_by_timestamp_renewal",
                message="a timestamp_renewal keeps the hash algorithm (RFC 4998 "
                "§5.2); changing it requires a hash_tree_renewal (§5.3)",
            )
        )
    return out


def _cover_findings(
    i: int, r: LtvRenewal, prev: LtvRenewal | None, covered: set[str], refs: set[str]
) -> list[LtvFinding]:
    out: list[LtvFinding] = []
    if prev is None:
        if r.parent is not None:
            out.append(
                LtvFinding(
                    renewal_index=i,
                    code="first_renewal_has_parent",
                    message="the first renewal must have parent null; a non-null "
                    "parent points at a renewal this chain does not contain",
                )
            )
    elif r.parent != prev.new_timestamp_ref:
        out.append(
            LtvFinding(
                renewal_index=i,
                code="missing_parent",
                message="parent is not the previous renewal's new_timestamp_ref; "
                "this renewal does not cover the one before it",
            )
        )
    if prev is not None and r.renewal_type == "timestamp_renewal":
        if r.covered_digest != prev.new_timestamp_ref:
            out.append(
                LtvFinding(
                    renewal_index=i,
                    code="covered_digest_mismatch",
                    message="a timestamp_renewal re-timestamps the previous archive "
                    "timestamp (RFC 4998 §5.2); covered_digest must equal it",
                )
            )
    if r.renewal_type == "hash_tree_renewal" and _digest_alg(r.covered_digest) != r.new_hash_alg:
        out.append(
            LtvFinding(
                renewal_index=i,
                code="covered_alg_mismatch",
                message="a hash_tree_renewal re-hashes the evidence under "
                f"new_hash_alg {r.new_hash_alg}, but covered_digest is "
                f"{_digest_alg(r.covered_digest)}",
            )
        )
    if r.renewal_type == "hash_tree_renewal" and r.covered_digest in covered:
        out.append(
            LtvFinding(
                renewal_index=i,
                code="covered_digest_reused",
                message="covered_digest was already covered by an earlier renewal; "
                "a hash-tree renewal must cover the state including the latest "
                "timestamp",
            )
        )
    if r.new_timestamp_ref in refs or r.new_timestamp_ref == r.covered_digest:
        out.append(
            LtvFinding(
                renewal_index=i,
                code="timestamp_ref_reused",
                message="new_timestamp_ref repeats an earlier timestamp or the "
                "digest it covers; the chain loops back on itself",
            )
        )
    return out


def _time_findings(i: int, r: LtvRenewal, prev: LtvRenewal | None) -> list[LtvFinding]:
    out: list[LtvFinding] = []
    deadline = _parse_deadline(r.renewed_before, field="renewed_before")
    if r.renewed_at is not None and _parse_instant(r.renewed_at, field="renewed_at") >= deadline:
        out.append(
            LtvFinding(
                renewal_index=i,
                code="renewed_at_not_before_deadline",
                message=f"renewed_at {r.renewed_at} is not before the sunset "
                f"{r.renewed_before} it claims to have beaten",
            )
        )
    if prev is None:
        return out
    if deadline < _parse_deadline(prev.renewed_before, field="renewed_before"):
        out.append(
            LtvFinding(
                renewal_index=i,
                code="renewed_before_not_monotonic",
                message=f"renewed_before {r.renewed_before} is earlier than the "
                f"previous renewal's {prev.renewed_before}",
            )
        )
    if prev.timestamp_expires_at is not None:
        expiry = _parse_deadline(prev.timestamp_expires_at, field="timestamp_expires_at")
        late_deadline = deadline > expiry
        late_at = (
            r.renewed_at is not None and _parse_instant(r.renewed_at, field="renewed_at") >= expiry
        )
        if late_deadline or late_at:
            out.append(
                LtvFinding(
                    renewal_index=i,
                    code="renewed_after_prior_expiry",
                    message="this renewal is not shown to precede the previous "
                    f"timestamp's recorded expiry {prev.timestamp_expires_at}",
                )
            )
    return out


def verify_ltv_chain(renewals: Sequence[LtvRenewal]) -> LtvVerification:
    """Walk an LTV renewal chain offline, linearly, reporting every fault.

    Never raises on a chain fault. Pure: no IO, no network, no clock. An empty
    chain is ``ok`` (nothing renewed yet); whether a renewal is *overdue* is a
    posture question (NF-340), not a chain fault.
    """
    if len(renewals) > MAX_RECORDS:
        finding = LtvFinding(
            renewal_index=MAX_RECORDS,
            code="too_many_records",
            message=f"chain exceeds {MAX_RECORDS} renewals; refusing to walk it",
        )
        return LtvVerification(
            renewal_count=len(renewals),
            covers_previous=False,
            hash_algs_ok=False,
            renewed_in_time=False,
            findings=[finding],
        )
    findings: list[LtvFinding] = []
    covered: set[str] = set()
    refs: set[str] = set()
    for i, r in enumerate(renewals):
        prev = renewals[i - 1] if i else None
        findings += _cover_findings(i, r, prev, covered, refs)
        findings += _alg_findings(i, r, prev)
        findings += _time_findings(i, r, prev)
        covered.add(r.covered_digest)
        refs.add(r.new_timestamp_ref)
    codes = {f.code for f in findings}
    return LtvVerification(
        renewal_count=len(renewals),
        covers_previous=not codes & _COVER_CODES,
        hash_algs_ok=not codes & _ALG_CODES,
        renewed_in_time=not codes & _TIME_CODES,
        findings=findings,
    )


def plan_ltv_renewal(
    facet: PreservationFacet,
    *,
    renewal_type: RenewalType,
    new_timestamp_ref: str,
    new_hash_alg: str,
    renewed_before: str,
    renewed_at: str,
    covered_digest: str | None = None,
    timestamp_expires_at: str | None = None,
) -> LtvRenewal:
    """Build the next renewal with ``parent`` derived from the chain.

    For a ``timestamp_renewal`` after the first, ``covered_digest`` defaults to
    the previous ``new_timestamp_ref`` (what RFC 4998 §5.2 covers). It is
    required for the first renewal and for every hash-tree renewal, where only
    the caller holds the bytes it was computed over.

    Raises:
        PreservationError: ``covered_digest`` missing where required; stored
            chain malformed.
        ValidationError: a field is malformed.
    """
    chain = ltv_chain_from_facet(facet)
    last = chain[-1] if chain else None
    if covered_digest is None:
        if last is None or renewal_type != "timestamp_renewal":
            raise PreservationError(
                "covered_digest is required for the first renewal and for a "
                "hash_tree_renewal (the digest of the evidence the new timestamp covers)"
            )
        covered_digest = last.new_timestamp_ref
    return LtvRenewal(
        renewal_type=renewal_type,
        covered_digest=covered_digest,
        new_timestamp_ref=new_timestamp_ref,
        new_hash_alg=new_hash_alg,
        renewed_before=renewed_before,
        parent=last.new_timestamp_ref if last else None,
        renewed_at=renewed_at,
        timestamp_expires_at=timestamp_expires_at,
    )


def append_ltv_renewal(
    facet: PreservationFacet,
    renewal: LtvRenewal,
    *,
    record_event: bool = True,
) -> PreservationFacet:
    """Append one renewal, returning a **new** facet (I-1).

    Refuses to extend a chain that already fails, or to append a renewal that
    would make it fail. With ``record_event`` and a ``renewed_at``, a
    ``timestamp renewal`` provenance event is appended.

    Raises:
        BrokenLtvChainError: the existing or resulting chain fails.
    """
    chain = ltv_chain_from_facet(facet)
    before = verify_ltv_chain(chain)
    if not before.ok:
        raise BrokenLtvChainError(
            "refusing to append to an LTV renewal chain that already fails verification",
            before,
        )
    after = verify_ltv_chain([*chain, renewal])
    if not after.ok:
        raise BrokenLtvChainError(
            "refusing to append a renewal that would break the LTV chain: "
            + "; ".join(f.code for f in after.findings),
            after,
        )
    serialized = [r.model_dump(mode="json", exclude_none=True) for r in (*chain, renewal)]
    # parent is required-but-nullable: keep an explicit null for the first renewal.
    for item, r in zip(serialized, (*chain, renewal), strict=True):
        item["parent"] = r.parent
    out = facet.model_copy(update={LTV_CHAIN_FIELD: serialized})
    if record_event and renewal.renewed_at is not None:
        out = append_provenance_event(out, provenance_event(LTV_RENEWAL_EVENT, renewal.renewed_at))
    return out


# ── Append-only guard across two facet versions ───────────────────────────


def verify_p3_append_only(before: PreservationFacet, after: PreservationFacet) -> None:
    """Assert ``after`` only appended to ``before``'s re-seal and LTV records (I-1).

    Raises:
        P3RewriteError: ``original_root`` changed, a list shrank, or an
            earlier entry was edited or reordered.
    """
    if before.original_root != after.original_root:
        raise P3RewriteError(
            f"original_root changed ({before.original_root} → {after.original_root}); "
            "the record now preserves a different object (ADR-0165 I-1)"
        )
    pairs: tuple[tuple[str, Sequence[BaseModel], Sequence[BaseModel]], ...] = (
        (
            CRYPTO_MIGRATION_FIELD,
            crypto_migrations_from_facet(before),
            crypto_migrations_from_facet(after),
        ),
        (LTV_CHAIN_FIELD, ltv_chain_from_facet(before), ltv_chain_from_facet(after)),
    )
    for key, old, new in pairs:
        if len(new) < len(old):
            raise P3RewriteError(
                f"{key} shrank from {len(old)} to {len(new)} entries (ADR-0165 I-1)"
            )
        for index, (a, b) in enumerate(zip(old, new, strict=False)):
            if a.model_dump(mode="json") != b.model_dump(mode="json"):
                raise P3RewriteError(
                    f"{key}[{index}] was rewritten; recorded entries are history "
                    "and are never edited (ADR-0165 I-1)"
                )
