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

"""Preservation anchor + fixity log — ADR-0165 D1/D4 P1 (NF-331, NF-335).

The time-axis layer. A capsule sealed in 2026 under a 10-, 20-, or 30-year
retention obligation will outlive its own cryptography, its own schema version
and possibly its own storage system. This module writes the *anchor* every
later preservation event appends to: an OAIS Preservation Description
Information record — Fixity (the bits are unaltered) plus Provenance (PREMIS
events describing what was done to the object and by whom) — bound into the
sealed capsule root, and an append-only log of the periodic fixity checks that
are the primary bit-rot detector (NDSA).

Four invariants from ADR-0165 shape every choice in this module:

- **I-1 Append-only / never break the original.** Every longevity operation
  adds an event referencing the prior state. ``original_root`` never changes,
  the anchor's ``fixity`` digest is never rewritten, and a fixity check is
  appended to the log, never overwritten. A later PASS does not erase an
  earlier FAIL: bit rot that was subsequently healed from a good copy is still
  evidence that the archive lost bits once.
- **I-2 References, digests and events only.** No private key (classic or
  PQC), no TSA key, no second copy of the archived bytes. Fixity is a digest
  *over* the already-stored artifact; this module never retains what it hashed.
- **I-3 Fail-open.** Absent preservation material means no facet, never an
  exception and never a blocked workload. A *verification* failure is surfaced
  by the caller at verify time, not raised at capture time.
- **I-4 Record-only, never adjudicated.** This module records that a fixity
  check ran and what it found. It never repairs bit rot, never re-digests an
  artifact to make a mismatch go away, and never asserts that an archive is
  durable, lawful, or accepted by a regulator.

**No Merkle tree is computed here, deliberately.** The repository carries two
mutually incompatible Merkle constructions — ``evidence/merkle.py`` (RFC 6962,
domain-separated leaf/inner prefixes) and ``trust/novaseal/merkle.py``
(pairwise with odd-duplicate padding) — and mixing leaves from one with the
other's combiner silently yields a wrong root. P1 needs no tree, which avoids
the question entirely: OAIS PDI Fixity is a single digest over a single sealed
artifact, and the fixity log is a flat chronological list, not a set requiring
an inclusion proof. So this module uses a plain ``hashlib.sha256`` per artifact
and computes no tree at all. When P3's RFC 4998 hash-tree renewal (NF-334)
needs one, it must pick one construction explicitly and say which.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

FACET_NAME = "preservation"
SCHEMA_VERSION = "0.1.0"

#: The one digest form the rest of the capsule uses. Matched strictly
#: (lower-case hex, exact length) so a truncated or upper-cased digest fails
#: loudly at construction rather than failing to match in 2035, during an audit,
#: when nobody who wrote it is still around to explain it.
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

#: An agent or artifact may be named by locator rather than by content — a
#: PREMIS Agent identifier, an https URL, an OCI ref. A URI *shape* is accepted;
#: nothing here dereferences it (P1 is offline, no network).
_URI_RE = re.compile(r"^[a-z][a-z0-9+.\-]*://\S+$")

#: A reference is an identifier, never a document. Anything longer is
#: overwhelmingly likely to be inlined content smuggled through a "ref" field,
#: which is exactly what I-2 exists to stop.
MAX_REF_LENGTH = 2048

#: Fixity algorithms this slice will record. Restricted on purpose: a fixity
#: digest whose algorithm a future verifier cannot reproduce is not evidence,
#: it is a string. `sha256` is what the capsule already uses; `sha512` and
#: `sha3-256` are here because RFC 4998 hash-tree renewal migrates *upward*
#: through stronger hashes, and a log that could not record the stronger one
#: would force P3 to break the schema. md5/sha1 are excluded: an archive
#: checked under a broken hash has not really been checked.
FixityAlg = Literal["sha256", "sha512", "sha3-256"]

#: What the fixity log says about the object as a whole.
#:
#: `not_checked` is a distinct state from `intact`, not a synonym for it.
#: An empty fixity log means nobody has looked — which is precisely the
#: situation periodic fixity exists to eliminate — and reporting that as
#: "intact" would launder an absence of evidence into a positive finding
#: (ADR-0165 I-4). Absent is not false, and absent is not true either.
FixityStatus = Literal["not_checked", "intact", "bit_rot_detected"]


# ── Errors ────────────────────────────────────────────────────────────────


class PreservationError(Exception):
    """Base class for every error this layer raises.

    Subclasses :class:`Exception` rather than :class:`ValueError`: a caller
    wrapping preservation work wants to catch *preservation* failures, and
    inheriting from ValueError would also swallow unrelated coercion errors
    raised from inside the same block.
    """


class InvalidDigestError(PreservationError):
    """Raised when a digest or reference is malformed."""


class PayloadCaptureError(PreservationError):
    """Raised when archived bytes arrive where a reference belongs.

    Distinct from :class:`InvalidDigestError` on purpose: a malformed digest is
    a caller mistake, while a payload reaching this boundary is an I-2
    violation, and the two want different fixes from whoever reads the
    traceback.
    """


class FixityLogRewriteError(PreservationError):
    """Raised when an operation would rewrite or truncate the fixity log.

    The load-bearing I-1 guard. A fixity log is only evidence because it is
    append-only: if a later clean check could drop an earlier
    ``matches_original: false``, the log would record the archive's *current*
    state rather than its *history*, and detected-then-healed bit rot would
    become indistinguishable from an archive that never rotted.
    """


# ── Reference and digest validation ───────────────────────────────────────


def _validate_ref(value: object, *, field: str) -> str:
    """Return ``value`` if it is an acceptable reference (digest or URI).

    Raises:
        PayloadCaptureError: if ``value`` is bytes, or is too long to be an
            identifier. Bytes are rejected rather than hashed on the caller's
            behalf: hashing here would make it effortless to hand this module
            the archived object itself and have it quietly do the right-looking
            thing, and the one time it did the wrong thing nobody would know
            the bytes had been in the process at all (I-2).
        InvalidDigestError: if ``value`` is neither ``sha256:<hex>`` nor a URI.
    """
    if isinstance(value, (bytes, bytearray, memoryview)):
        raise PayloadCaptureError(
            f"{field} must be a reference (sha256 digest or URI), not raw bytes; "
            "digest the artifact yourself with digest_artifact() and pass the "
            "result (ADR-0165 I-2 — the preservation layer never holds a second "
            "copy of the archived bytes)"
        )
    if not isinstance(value, str):
        raise InvalidDigestError(f"{field} must be a string reference")
    if len(value) > MAX_REF_LENGTH:
        raise PayloadCaptureError(
            f"{field} is {len(value)} chars, over the {MAX_REF_LENGTH}-char "
            "reference limit; this looks like inlined archived content, not a ref"
        )
    if not (_DIGEST_RE.match(value) or _URI_RE.match(value)):
        raise InvalidDigestError(
            f"{field} must be 'sha256:<64 hex>' or a URI, got {value!r}"
        )
    return value


def _validate_digest(value: object, *, field: str) -> str:
    """Return ``value`` if it is a ``sha256:`` content digest.

    Stricter than :func:`_validate_ref`: a root or a fixity digest binds by
    *content identity*, and a URI would not be a binding at all — it names
    where something lives, not what it is, and the thing it names can be
    replaced underneath the capsule. Over a 30-year window that is not a
    hypothetical.
    """
    ref = _validate_ref(value, field=field)
    if not _DIGEST_RE.match(ref):
        raise InvalidDigestError(
            f"{field} must be a content digest 'sha256:<64 hex>', not a locator; "
            f"got {ref!r}"
        )
    return ref


def digest_artifact(artifact: str | bytes) -> str:
    """Return the ``sha256:`` fixity digest of a sealed artifact.

    Plain ``hashlib.sha256`` over the artifact's bytes — no Merkle leaf prefix,
    no tree, no domain separation (see the module docstring for why). Emitted
    in the same ``sha256:<hex>`` form the rest of the capsule uses, so a
    verifier does not have to know which subsystem wrote it.

    The artifact is hashed and discarded; nothing here retains it (I-2).
    """
    raw = artifact.encode("utf-8") if isinstance(artifact, str) else artifact
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


# ── Objects ───────────────────────────────────────────────────────────────


class Fixity(BaseModel):
    """OAIS PDI Fixity — the digest asserting the bits are unaltered.

    Recorded once, at anchor time, and never rewritten. Re-computed digests go
    into the fixity log as new checks; overwriting this field would erase the
    baseline every later check is compared against (I-1).
    """

    model_config = ConfigDict(extra="allow")

    alg: FixityAlg = "sha256"
    digest: str

    @field_validator("digest", mode="before")
    @classmethod
    def _check_digest(cls, v: object) -> str:
        # mode="before" on every digest field: Pydantic's own str coercion would
        # otherwise reject bytes first, with a generic "not a valid string" that
        # tells the caller nothing about *why* bytes are forbidden here (I-2).
        return _validate_digest(v, field="digest")


class ProvenanceEvent(BaseModel):
    """One PREMIS-style preservation event: what happened, when, by whom.

    ``agent_ref`` is a PREMIS Agent *identifier* — a digest or URI naming the
    node, tool, or custodian — never a person's name or a credential (I-2,
    ADR-0009).
    """

    model_config = ConfigDict(extra="allow")

    event: str
    at: str
    agent_ref: str | None = None

    @field_validator("agent_ref", mode="before")
    @classmethod
    def _check_agent_ref(cls, v: object) -> str | None:
        if v is None:
            return None
        return _validate_ref(v, field="agent_ref")

    @field_validator("event")
    @classmethod
    def _check_event(cls, v: str) -> str:
        if not v.strip():
            raise ValueError(
                "event must be non-empty; an unnamed preservation event cannot "
                "be reconciled against a migration chain later (NF-331)"
            )
        return v


class FixityCheck(BaseModel):
    """One periodic fixity check — NF-335, the bit-rot detector.

    ``matches_original`` is a required bool with no default. A check that ran
    always produced a verdict, and defaulting it either way would let a check
    that never happened, or one whose result was lost, enter the log wearing a
    result it never had. A check nobody performed must simply be absent from
    the log — absent is not false, and it is certainly not true (I-4).
    """

    model_config = ConfigDict(extra="allow")

    checked_at: str
    alg: FixityAlg = "sha256"
    digest: str
    matches_original: bool
    agent_ref: str | None = None

    @field_validator("digest", mode="before")
    @classmethod
    def _check_digest(cls, v: object) -> str:
        return _validate_digest(v, field="digest")

    @field_validator("agent_ref", mode="before")
    @classmethod
    def _check_agent_ref(cls, v: object) -> str | None:
        if v is None:
            return None
        return _validate_ref(v, field="agent_ref")


class PreservationFacet(BaseModel):
    """The optional ``facets.preservation`` anchor (NF-331, I-1).

    Every later preservation object — the format-migration chain (P2), the
    crypto re-seal and LTV renewal chains (P3), conformance, obsolescence and
    custody (P4) — appends to this anchor. ``extra="allow"`` is what lets those
    slices land without a schema break.
    """

    model_config = ConfigDict(extra="allow")

    schema_version: str = SCHEMA_VERSION
    preservation_id: str
    #: The sealed capsule root this chain preserves. It never changes, for the
    #: whole life of the chain — a migration that altered it would have
    #: preserved a different object than the one it claims to preserve (I-1).
    original_root: str
    fixity: Fixity
    provenance_events: list[ProvenanceEvent] = Field(default_factory=list)
    #: Append-only. See :func:`append_fixity_check` for the guard.
    fixity_log: list[FixityCheck] = Field(default_factory=list)
    #: Digest of the sealed capsule root this facet is bound into. Optional:
    #: the root is only known once the capsule is sealed, and an anchor built
    #: during a run legitimately does not have it yet.
    bound_root: str | None = None

    @field_validator("original_root", mode="before")
    @classmethod
    def _check_original_root(cls, v: object) -> str:
        return _validate_digest(v, field="original_root")

    @field_validator("bound_root", mode="before")
    @classmethod
    def _check_bound_root(cls, v: object) -> str | None:
        if v is None:
            return None
        return _validate_digest(v, field="bound_root")


# ── Construction ──────────────────────────────────────────────────────────


def provenance_event(
    event: str, at: str, *, agent_ref: str | None = None
) -> ProvenanceEvent:
    """Build one PREMIS-style provenance event."""
    return ProvenanceEvent(event=event, at=at, agent_ref=agent_ref)


def build_anchor(
    preservation_id: str,
    original_root: str,
    *,
    fixity_digest: str,
    fixity_alg: FixityAlg = "sha256",
    provenance_events: Iterable[ProvenanceEvent] = (),
    fixity_log: Iterable[FixityCheck] = (),
    bound_root: str | None = None,
) -> PreservationFacet:
    """Assemble the preservation anchor (NF-331).

    Neither list is sorted, unlike the other facet builders in this codebase.
    A preservation chain's order *is* the record: these are append-only
    histories, and re-ordering them by timestamp would silently repair a log
    whose entries arrived out of order — hiding exactly the anomaly (a check
    recorded before the event it checks) that a preservation auditor wants to
    see. Callers append in the order things happened; this module preserves
    that order verbatim.
    """
    return PreservationFacet(
        preservation_id=preservation_id,
        original_root=original_root,
        fixity=Fixity(alg=fixity_alg, digest=fixity_digest),
        provenance_events=list(provenance_events),
        fixity_log=list(fixity_log),
        bound_root=bound_root,
    )


def attach_facet(
    capsule: dict[str, Any], facet: PreservationFacet | None
) -> dict[str, Any]:
    """Attach the preservation anchor to a capsule dict, additively.

    Writes nothing when ``facet`` is None: a run with no preservation material
    must be byte-identical to one captured before this feature existed (I-1,
    I-3). Returns a new dict; the input is not mutated.
    """
    if facet is None:
        return capsule
    out = dict(capsule)
    facets = dict(out.get("facets") or {})
    # exclude_none so an absent agent_ref or bound_root is *absent*, not `null`.
    # In a layer whose whole point is that "not recorded" differs from
    # "recorded as nothing", that distinction has to survive serialisation.
    facets[FACET_NAME] = facet.model_dump(exclude_none=True)
    out["facets"] = facets
    return out


def facet_from_capsule(capsule: dict[str, Any]) -> PreservationFacet | None:
    """Read the preservation anchor back out of a capsule dict.

    Returns None when the capsule has no anchor — the overwhelmingly common
    case, and not an error (I-3).
    """
    facets = capsule.get("facets")
    if not isinstance(facets, dict):
        return None
    block = facets.get(FACET_NAME)
    if not isinstance(block, dict):
        return None
    return PreservationFacet.model_validate(block)


# ── Fixity: append-only, bit-rot surfaced, never repaired ─────────────────


def check_fixity(
    facet: PreservationFacet,
    artifact: str | bytes,
    *,
    checked_at: str,
    agent_ref: str | None = None,
) -> PreservationFacet:
    """Re-digest the stored artifact and append the result to the fixity log.

    Returns a **new** facet; ``facet`` is not mutated.

    The verdict is recorded whatever it is. On a mismatch this appends
    ``matches_original: false`` and leaves ``facet.fixity`` — the baseline —
    exactly as it was. It deliberately does **not** update the anchor's digest
    to the newly observed one: that would "repair" the record by redefining
    what the object is supposed to be, converting detected corruption into a
    silently accepted new baseline. NovaFabric records bit rot; restoring from
    a good copy is the storage layer's job (ADR-0031, I-4).

    Raises:
        PreservationError: if the anchor's fixity algorithm is one this slice
            cannot re-compute. Raised rather than recorded as a mismatch: a
            check that could not run did not find corruption, and logging it as
            ``matches_original: false`` would fabricate a bit-rot finding.
    """
    if facet.fixity.alg != "sha256":
        raise PreservationError(
            f"cannot re-compute fixity under {facet.fixity.alg!r}; P1 computes "
            "sha256 only. Recording this as a mismatch would fabricate a "
            "bit-rot finding for a check that never ran (ADR-0165 I-4)"
        )
    observed = digest_artifact(artifact)
    return append_fixity_check(
        facet,
        FixityCheck(
            checked_at=checked_at,
            alg="sha256",
            digest=observed,
            matches_original=observed == facet.fixity.digest,
            agent_ref=agent_ref,
        ),
    )


def append_fixity_check(
    facet: PreservationFacet, check: FixityCheck
) -> PreservationFacet:
    """Append one check to the fixity log, returning a new facet.

    The only sanctioned way to grow the log. Nothing else in this module
    assigns to ``fixity_log``, so append-only holds because there is no other
    door, not because callers are asked to be careful.
    """
    return facet.model_copy(update={"fixity_log": [*facet.fixity_log, check]})


def append_provenance_event(
    facet: PreservationFacet, event: ProvenanceEvent
) -> PreservationFacet:
    """Append one PREMIS provenance event, returning a new facet."""
    return facet.model_copy(
        update={"provenance_events": [*facet.provenance_events, event]}
    )


def verify_append_only(before: PreservationFacet, after: PreservationFacet) -> None:
    """Assert ``after`` only appended to ``before`` (I-1).

    The guard a verifier runs against two versions of the same chain — the one
    it holds and the one an archive hands back after a decade of custody. It
    checks three things, each a way an append-only log can be quietly
    falsified: the anchor still preserves the same original, the baseline
    digest was not redefined, and every prior check survives unchanged at its
    original index.

    Raises:
        FixityLogRewriteError: on any of the three.
    """
    if before.original_root != after.original_root:
        raise FixityLogRewriteError(
            "original_root changed: a preservation chain that re-anchors is "
            f"preserving a different object ({before.original_root} → "
            f"{after.original_root}); ADR-0165 I-1"
        )
    if before.fixity.digest != after.fixity.digest:
        raise FixityLogRewriteError(
            "fixity baseline was rewritten; a re-digest that redefines the "
            "baseline turns detected corruption into an accepted new normal "
            "(ADR-0165 I-1/I-4 — record bit rot, never repair it)"
        )
    if len(after.fixity_log) < len(before.fixity_log):
        raise FixityLogRewriteError(
            f"fixity log shrank from {len(before.fixity_log)} to "
            f"{len(after.fixity_log)} entries; the log is append-only"
        )
    for index, (old, new) in enumerate(zip(before.fixity_log, after.fixity_log)):
        if old != new:
            # Compared whole, not just on `matches_original`: rewriting a prior
            # check's timestamp or agent is the same falsification, and is
            # harder to spot precisely because the verdict still reads FAIL.
            raise FixityLogRewriteError(
                f"fixity log entry {index} was rewritten; a prior check is "
                "evidence of the archive's history and is never edited "
                "(ADR-0165 I-1)"
            )


def fixity_status(facet: PreservationFacet) -> FixityStatus:
    """Summarise the fixity log — ``not_checked`` when nobody has looked.

    ``bit_rot_detected`` is **sticky**: one historical ``matches_original:
    false`` keeps this status even if every subsequent check passes. That is
    the point. An archive that lost bits and was restored from a good copy is
    materially different from one that never lost any, and a status that let a
    later PASS erase the FAIL would destroy the only record of the difference.
    Callers wanting "is it intact *right now*" should read the last entry.
    """
    if not facet.fixity_log:
        return "not_checked"
    if any(not check.matches_original for check in facet.fixity_log):
        return "bit_rot_detected"
    return "intact"


def detected_bit_rot(facet: PreservationFacet) -> list[FixityCheck]:
    """Return every check that found corruption, in the order recorded.

    Surfaced for the caller to act on — a CLI would exit non-zero here (NF-335).
    This module neither exits nor repairs (I-4).
    """
    return [check for check in facet.fixity_log if not check.matches_original]


def verify_anchor_binding(facet: PreservationFacet, artifact: str | bytes) -> bool:
    """Re-verify the anchor's baseline digest against the stored artifact.

    Returns False for an anchor whose algorithm this slice cannot re-compute,
    rather than True: an unverifiable binding is exactly the case a binding
    check exists to surface, and returning True would let the anchors with
    nothing checkable sail through.
    """
    if facet.fixity.alg != "sha256":
        return False
    return facet.fixity.digest == digest_artifact(artifact)


def scan_for_payloads(values: Sequence[object]) -> None:
    """Raise if any value is artifact-shaped rather than reference-shaped (I-2).

    Exposed so a caller assembling an anchor from untrusted archive metadata
    can check before constructing, rather than discovering the problem as a
    validation error halfway through a model.
    """
    for index, value in enumerate(values):
        _validate_ref(value, field=f"reference[{index}]")
