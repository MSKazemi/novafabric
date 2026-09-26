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

"""Transitive trust path — ADR-0168 D2 second half, P2 (NF-363). **Experimental.**

"A trusts C's evidence via B" becomes an offline-walkable chain of signed
delegation statements (OpenID-Federation trust-chain shape) instead of an
assertion. Each hop is ``{from_org, to_org, statement_digest, anchor_digest,
signature}`` plus the public key the hop vouches for (``subject_public_key``)
and an optional signed depth constraint (``max_path_length``).

Walk direction and key resolution
---------------------------------
The path is ordered **anchor first**. The verifier — never the path — supplies
the pinned anchors (:class:`PinnedAnchor`: an org id bound to a public key).
Hop 0 must name a pinned anchor both by ``anchor_digest`` and by ``from_org``,
and its signature must verify under **that pinned key**. Every later hop ``i``
must be signed by the key hop ``i-1`` vouched for, and its ``from_org`` must be
hop ``i-1``'s ``to_org``. A key carried by a hop is only ever used to verify
the *next* hop, so no hop can authenticate itself, and an anchor asserted only
by the path itself never terminates anything.

Every hop carries the same ``anchor_digest``: substituting a different anchor
mid-path is an anchor-mismatched hop, not a second chain.

Fail closed
-----------
Verification never raises on hostile path content; it returns a
:class:`TrustPathReport` whose :attr:`TrustPathReport.path_walk_ok` is true only
when the path is acyclic, terminates at a pinned anchor, has no broken hop, and
touches no verifier-supplied revoked subject. The walk stops at the first
broken hop and names it. Exceeding a delegation depth — the signed per-hop
``max_path_length`` or the verifier's ``max_depth`` policy — is **flagged**
(``delegation_depth_exceeded``) and recorded, never fatal (spec §3.7). The two
are reported apart (``signed_depth_flags`` vs ``policy_depth_exceeded``). A
verifier that wants OpenID-Federation semantics — a delegate told "no further
delegation" who delegates anyway breaks the path — passes ``strict_depth=True``,
which makes a *signed*-constraint overrun fatal (``signed_depth_exceeded``);
the verifier's own ``max_depth`` policy stays a flag either way.

Crypto
------
Ed25519 and ECDSA P-256/SHA-256 only, via ``cryptography`` (a core
dependency). The signature algorithm is **never** read from the path: it is
implied by the signer's key type, which for hop 0 comes from the verifier's
pin and afterwards from the signed ``subject_public_key`` of the previous hop.
That closes algorithm-confusion attacks. Keys are identified by
``sha256:`` over their DER SubjectPublicKeyInfo, which includes the algorithm
OID, so an Ed25519 key and a P-256 key can never share a digest.

Canonical statement bytes follow :mod:`novafabric.trust.delegation`:
``json.dumps(obj, sort_keys=True, separators=(",", ":"))`` in UTF-8, with a
``type`` domain separator so a trust-path statement can never be replayed as a
delegation grant or vice versa.

What a valid path is not
------------------------
A valid path proves the *delegation chain composes*. It is not a statement
that C's evidence is correct, and NovaFabric is never the anchor, the CA, or
the trust authority — it walks paths other orgs signed (ADR-0168 I-1/I-4).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from novafabric.federation.facet import (
    FACET_NAME,
    SCHEMA_VERSION,
    FederationError,
    PayloadCrossedBoundaryError,
    _validate_digest,
)

__all__ = [
    "IN_MISSION_BOUNDARY",
    "MAX_HOPS",
    "MAX_ORG_ID_LENGTH",
    "STATEMENT_TYPE",
    "TRUST_PATH_KEY",
    "PinnedAnchor",
    "TrustPath",
    "TrustPathError",
    "TrustPathHop",
    "TrustPathReport",
    "attach_trust_path",
    "key_digest",
    "parse_trust_path",
    "sign_hop",
    "statement_bytes",
    "structural_summary",
    "trust_path_from_capsule",
    "verify_trust_path",
]

#: Printed by every ``nova trust-path`` output (spec §3.4).
IN_MISSION_BOUNDARY = (
    "NovaFabric records and verifies evidence about cross-org trust; it is never a "
    "CA, PKI root, identity provider, notary, transparency service or trust "
    "authority. A valid trust path proves the delegation chain composes to a "
    "verifier-pinned anchor - not that the foreign evidence is correct (ADR-0168)."
)

#: Facet key under ``facets.federation`` (spec §4.2).
TRUST_PATH_KEY = "trust_path"

#: Domain separator inside every signed statement.
STATEMENT_TYPE = "novafabric.federation.trust-path-statement.v1"

#: Upper bound on hops. OpenID-Federation chains are typically 2-4 long; a
#: path far longer than this is a resource-exhaustion attempt, not a federation.
MAX_HOPS = 16

#: Upper bound on an org identifier (opaque id / key digest / trust domain).
MAX_ORG_ID_LENGTH = 256

#: Upper bound on a hop's signed ``max_path_length`` constraint.
MAX_PATH_LENGTH_CONSTRAINT = MAX_HOPS

#: Base64 caps. Ed25519 SPKI DER is 44 bytes and a P-256 SPKI DER is 91; an
#: Ed25519 signature is 64 bytes and a DER ECDSA P-256 signature at most 72.
_MAX_KEY_B64 = 256
_MAX_SIG_B64 = 128

#: Visible ASCII, no whitespace — an org id is an identifier, never prose.
_ORG_ID_RE = re.compile(r"[\x21-\x7e]{1,%d}" % MAX_ORG_ID_LENGTH)
_B64_RE = re.compile(r"[A-Za-z0-9+/]+={0,2}")


class TrustPathError(FederationError):
    """A trust path is malformed (shape, size, encoding) or an input is invalid.

    Raised by parsing and signing helpers. :func:`verify_trust_path` itself
    never raises on hostile path content; it reports a failed walk instead.
    """


# ── Encoding helpers ──────────────────────────────────────────────────────


def _strict_b64decode(value: str, *, field: str, cap: int) -> bytes:
    """Decode standard base64 strictly (no whitespace, no junk, bounded)."""
    if len(value) > cap:
        raise TrustPathError(f"{field} is {len(value)} chars, over the {cap}-char cap")
    if not _B64_RE.fullmatch(value) or len(value) % 4 != 0:
        raise TrustPathError(f"{field} is not strict standard base64")
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:  # pragma: no cover - regex pre-screens
        raise TrustPathError(f"{field} is not strict standard base64") from exc


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


_PublicKey = Ed25519PublicKey | ec.EllipticCurvePublicKey


def _load_spki(der: bytes) -> _PublicKey:
    """Load a DER SubjectPublicKeyInfo as an Ed25519 or P-256 public key.

    Raises:
        TrustPathError: for undecodable DER or any other key type/curve.
    """
    try:
        key = serialization.load_der_public_key(der)
    except (ValueError, UnsupportedAlgorithm) as exc:
        raise TrustPathError("public key is not a valid DER SubjectPublicKeyInfo") from exc
    if isinstance(key, Ed25519PublicKey):
        return key
    if isinstance(key, ec.EllipticCurvePublicKey) and isinstance(key.curve, ec.SECP256R1):
        return key
    raise TrustPathError(
        f"unsupported public key type {type(key).__name__}; only Ed25519 and "
        "ECDSA P-256 are accepted"
    )


def _spki_der(key: _PublicKey) -> bytes:
    return key.public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )


def key_digest(public_key: _PublicKey | bytes) -> str:
    """Return ``sha256:<hex>`` over a public key's DER SubjectPublicKeyInfo.

    Accepts a key object or its DER SPKI bytes. The SPKI includes the
    algorithm OID, so keys of different algorithms never collide.
    """
    der = public_key if isinstance(public_key, bytes) else _spki_der(public_key)
    return f"sha256:{hashlib.sha256(der).hexdigest()}"


def _verify_signature(key: _PublicKey, signature: bytes, message: bytes) -> bool:
    """Verify *signature* over *message* under *key*; the key type fixes the algorithm."""
    try:
        if isinstance(key, Ed25519PublicKey):
            key.verify(signature, message)
        else:
            key.verify(signature, message, ec.ECDSA(hashes.SHA256()))
    except (InvalidSignature, ValueError):
        return False
    return True


def _validate_org(value: object, *, field: str) -> str:
    if isinstance(value, (bytes, bytearray, memoryview, list, tuple, dict)):
        raise PayloadCrossedBoundaryError(
            f"{field} must be an opaque org identifier string (ADR-0168 I-2)"
        )
    if not isinstance(value, str) or not _ORG_ID_RE.fullmatch(value):
        raise TrustPathError(
            f"{field} must be 1-{MAX_ORG_ID_LENGTH} visible ASCII characters with no whitespace"
        )
    return value


# ── Models ────────────────────────────────────────────────────────────────


class TrustPathHop(BaseModel):
    """One signed delegation statement: ``from_org`` vouches for ``to_org``'s key.

    ``extra="forbid"``: an unrecognised field on a hop is unsigned material a
    verifier would have to ignore, and ignoring attacker-supplied fields is
    how verifiers get confused. Unknown shape fails closed.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    from_org: str
    to_org: str
    #: ``sha256:`` of :func:`statement_bytes` for this hop.
    statement_digest: str
    #: Digest (:func:`key_digest`) of the pinned anchor this hop resolves toward.
    anchor_digest: str
    #: Base64 signature by ``from_org``'s key over the statement bytes.
    signature: str
    #: Base64 DER SPKI of the key ``from_org`` vouches ``to_org`` holds.
    subject_public_key: str
    #: Signed OpenID-Federation-style constraint: how many further hops may
    #: follow this one. ``None`` means unconstrained by this hop.
    max_path_length: int | None = Field(default=None, ge=0, le=MAX_PATH_LENGTH_CONSTRAINT)

    @field_validator("from_org", "to_org", mode="before")
    @classmethod
    def _check_org(cls, v: object, info: Any) -> str:
        return _validate_org(v, field=info.field_name)

    @field_validator("statement_digest", "anchor_digest", mode="before")
    @classmethod
    def _check_digest(cls, v: object, info: Any) -> str:
        return _validate_digest(v, field=info.field_name)

    @field_validator("signature", mode="before")
    @classmethod
    def _check_signature(cls, v: object) -> str:
        if not isinstance(v, str):
            raise TrustPathError("signature must be a base64 string")
        _strict_b64decode(v, field="signature", cap=_MAX_SIG_B64)
        return v

    @field_validator("subject_public_key", mode="before")
    @classmethod
    def _check_subject_key(cls, v: object) -> str:
        if not isinstance(v, str):
            raise TrustPathError("subject_public_key must be a base64 string")
        if "PRIVATE" in v:
            raise PayloadCrossedBoundaryError(
                "subject_public_key contains private key material; a private key "
                "never leaves the org that holds it (ADR-0168 I-2)"
            )
        _strict_b64decode(v, field="subject_public_key", cap=_MAX_KEY_B64)
        return v


class TrustPath(BaseModel):
    """An ordered, anchor-first list of hops (1..:data:`MAX_HOPS`)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    hops: tuple[TrustPathHop, ...] = Field(min_length=1, max_length=MAX_HOPS)


@dataclass(frozen=True)
class PinnedAnchor:
    """A trust anchor pinned **by the verifier**: an org id bound to a public key.

    Never derived from the path under verification — that is the property
    that stops a path from vouching for its own root.
    """

    org: str
    spki_der: bytes

    def __post_init__(self) -> None:
        _validate_org(self.org, field="anchor org")
        _load_spki(self.spki_der)

    @property
    def digest(self) -> str:
        """The anchor's :func:`key_digest`."""
        return key_digest(self.spki_der)

    @classmethod
    def from_public_key(cls, org: str, public_key: _PublicKey) -> PinnedAnchor:
        """Pin *public_key* (Ed25519 or P-256) under *org*."""
        return cls(org=org, spki_der=_spki_der(public_key))

    @classmethod
    def from_pem(cls, org: str, pem: bytes) -> PinnedAnchor:
        """Pin a PEM ``PUBLIC KEY`` under *org*.

        Raises:
            PayloadCrossedBoundaryError: if the PEM holds a private key.
            TrustPathError: if it is not an Ed25519/P-256 public key.
        """
        if b"PRIVATE KEY" in pem:
            raise PayloadCrossedBoundaryError(
                "anchor file holds a private key; pin the public key only (ADR-0168 I-2)"
            )
        try:
            key = serialization.load_pem_public_key(pem)
        except (ValueError, UnsupportedAlgorithm) as exc:
            raise TrustPathError("anchor is not a PEM public key") from exc
        der = key.public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        return cls(org=org, spki_der=der)


class TrustPathReport(BaseModel):
    """Outcome of :func:`verify_trust_path`. Deterministic; JSON-serialisable."""

    model_config = ConfigDict(frozen=True)

    path_walk_ok: bool
    acyclic: bool
    terminates_at_anchor: bool
    no_broken_hop: bool
    delegation_depth_exceeded: bool
    path_touches_revoked: bool
    hop_count: int
    #: Index of the first broken hop, when there is one.
    broken_hop: int | None = None
    #: Machine-readable reason code for the first failure.
    reason: str | None = None
    detail: str | None = None
    #: Hop indices whose depth constraint (signed or verifier policy) was exceeded.
    #: The union of ``signed_depth_flags`` and, when ``policy_depth_exceeded``,
    #: the index of the first hop beyond the verifier's ``max_depth``.
    depth_flags: tuple[int, ...] = ()
    #: Hop indices whose own *signed* ``max_path_length`` the path overruns.
    signed_depth_flags: tuple[int, ...] = ()
    #: Whether the path is longer than the verifier's ``max_depth`` policy.
    policy_depth_exceeded: bool = False
    #: Whether the walk ran with ``strict_depth`` (signed overrun is fatal).
    strict_depth: bool = False
    #: Under ``strict_depth``: the first hop issued beyond a signed allowance.
    depth_violation_hop: int | None = None
    #: Revoked subjects (org ids / key digests) the path transits.
    revoked_hits: tuple[str, ...] = ()
    anchor_org: str | None = None
    anchor_digest: str | None = None
    #: Only set when the whole walk succeeded.
    leaf_org: str | None = None
    leaf_key_digest: str | None = None


# ── Statements and signing ────────────────────────────────────────────────


def statement_bytes(
    *,
    from_org: str,
    to_org: str,
    subject_public_key: str,
    anchor_digest: str,
    max_path_length: int | None,
) -> bytes:
    """Deterministic bytes a hop's signer signs (delegation.py canonical JSON)."""
    obj = {
        "type": STATEMENT_TYPE,
        "from_org": from_org,
        "to_org": to_org,
        "subject_public_key": subject_public_key,
        "anchor_digest": anchor_digest,
        "max_path_length": max_path_length,
    }
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _hop_statement(hop: TrustPathHop) -> bytes:
    return statement_bytes(
        from_org=hop.from_org,
        to_org=hop.to_org,
        subject_public_key=hop.subject_public_key,
        anchor_digest=hop.anchor_digest,
        max_path_length=hop.max_path_length,
    )


def sign_hop(
    signer_key: Ed25519PrivateKey | ec.EllipticCurvePrivateKey,
    *,
    from_org: str,
    to_org: str,
    subject_public_key: _PublicKey,
    anchor_digest: str,
    max_path_length: int | None = None,
) -> TrustPathHop:
    """Build a hop in which *from_org* (holding *signer_key*) vouches for *to_org*.

    Run by the delegating org with its **own** key, locally; NovaFabric never
    holds another org's key and this helper never persists one. Used by
    tooling and tests to produce statements a verifier can walk.
    """
    subject_b64 = _b64(_spki_der(_load_spki(_spki_der(subject_public_key))))
    hop_fields: dict[str, Any] = {
        "from_org": _validate_org(from_org, field="from_org"),
        "to_org": _validate_org(to_org, field="to_org"),
        "subject_public_key": subject_b64,
        "anchor_digest": _validate_digest(anchor_digest, field="anchor_digest"),
        "max_path_length": max_path_length,
    }
    message = statement_bytes(**hop_fields)
    if isinstance(signer_key, Ed25519PrivateKey):
        signature = signer_key.sign(message)
    elif isinstance(signer_key, ec.EllipticCurvePrivateKey) and isinstance(
        signer_key.curve, ec.SECP256R1
    ):
        signature = signer_key.sign(message, ec.ECDSA(hashes.SHA256()))
    else:
        raise TrustPathError("signer_key must be an Ed25519 or ECDSA P-256 private key")
    return TrustPathHop(
        statement_digest=f"sha256:{hashlib.sha256(message).hexdigest()}",
        signature=_b64(signature),
        **hop_fields,
    )


# ── Parsing and facet I/O ─────────────────────────────────────────────────


def parse_trust_path(raw: object) -> TrustPath:
    """Parse the spec-shaped list of hop dicts (or a ``{"hops": [...]}`` mapping).

    Raises:
        TrustPathError: on any shape, size, or encoding problem (fail closed).
            :class:`PayloadCrossedBoundaryError` when key material or payload
            appears where a reference belongs.
    """
    if isinstance(raw, Mapping):
        raw = raw.get("hops")
    if not isinstance(raw, (list, tuple)):
        raise TrustPathError("trust_path must be a list of hops")
    if len(raw) > MAX_HOPS:
        raise TrustPathError(f"trust_path has {len(raw)} hops, over the {MAX_HOPS}-hop cap")
    try:
        return TrustPath(hops=tuple(raw))
    except (PayloadCrossedBoundaryError, TrustPathError):
        # Our validators raise non-ValueError exceptions, which Pydantic lets
        # propagate unwrapped; an I-2 violation surfaces as itself.
        raise
    except FederationError as exc:
        raise TrustPathError(f"malformed trust_path: {exc}") from exc
    except ValidationError as exc:
        first = exc.errors()[0]
        loc = ".".join(str(p) for p in first.get("loc", ()))
        raise TrustPathError(f"malformed trust_path at {loc}: {first.get('msg')}") from exc


def trust_path_from_capsule(capsule: Mapping[str, Any]) -> TrustPath | None:
    """Read ``facets.federation.trust_path``; None when absent (fail-open, I-3).

    Raises:
        TrustPathError: when present but malformed.
    """
    facets = capsule.get("facets")
    if not isinstance(facets, Mapping):
        return None
    block = facets.get(FACET_NAME)
    if not isinstance(block, Mapping) or TRUST_PATH_KEY not in block:
        return None
    return parse_trust_path(block[TRUST_PATH_KEY])


def attach_trust_path(capsule: dict[str, Any], path: TrustPath | None) -> dict[str, Any]:
    """Return a copy of *capsule* with the path under ``facets.federation``.

    ``None`` returns *capsule* unchanged (I-3). The rest of the federation
    facet is preserved; the input is not mutated.
    """
    if path is None:
        return capsule
    out = dict(capsule)
    facets = dict(out.get("facets") or {})
    block = dict(facets.get(FACET_NAME) or {"schema_version": SCHEMA_VERSION})
    block[TRUST_PATH_KEY] = [hop.model_dump(exclude_none=True) for hop in path.hops]
    facets[FACET_NAME] = block
    out["facets"] = facets
    return out


# ── Verification ──────────────────────────────────────────────────────────


def _path_is_acyclic(path: TrustPath) -> bool:
    """Structural cycle check over org ids: anchor org + every ``to_org`` unique."""
    orgs = [path.hops[0].from_org, *(hop.to_org for hop in path.hops)]
    return len(orgs) == len(set(orgs))


def _signed_depth_flags(path: TrustPath) -> tuple[int, ...]:
    """Hop indices whose own signed ``max_path_length`` the rest of the path overruns."""
    n = len(path.hops)
    return tuple(
        i
        for i, hop in enumerate(path.hops)
        if hop.max_path_length is not None and n - 1 - i > hop.max_path_length
    )


def _first_signed_violation(path: TrustPath, signed_flags: tuple[int, ...]) -> int | None:
    """The first hop issued beyond any signed allowance (``i + max_path_length + 1``)."""
    beyond: list[int] = []
    for i in signed_flags:
        limit = path.hops[i].max_path_length
        if limit is not None:
            beyond.append(i + limit + 1)
    return min(beyond) if beyond else None


def _revoked_hits(path: TrustPath, revoked: frozenset[str]) -> tuple[str, ...]:
    if not revoked:
        return ()
    subjects: list[str] = [path.hops[0].from_org, path.hops[0].anchor_digest]
    for hop in path.hops:
        subjects.append(hop.to_org)
        try:
            der = _strict_b64decode(
                hop.subject_public_key, field="subject_public_key", cap=_MAX_KEY_B64
            )
        except TrustPathError:  # pragma: no cover - validated at parse
            continue
        subjects.append(key_digest(der))
    return tuple(sorted({s for s in subjects if s in revoked}))


def verify_trust_path(
    path: TrustPath,
    *,
    pinned_anchors: Iterable[PinnedAnchor],
    max_depth: int | None = None,
    revoked: Iterable[str] = (),
    strict_depth: bool = False,
) -> TrustPathReport:
    """Walk *path* offline against verifier-pinned anchors; never raises on path content.

    Args:
        path: The parsed path, anchor first.
        pinned_anchors: The verifier's own pins. The path cannot add to them.
        max_depth: Optional verifier policy on total hops; exceeding it is
            flagged, not fatal.
        revoked: Verifier-supplied revoked subjects (org ids or key digests).
            Any hit sets ``path_touches_revoked`` and fails the walk.
        strict_depth: When true, overrunning a hop's *signed*
            ``max_path_length`` fails an otherwise-valid walk
            (``signed_depth_exceeded``) — OpenID Federation semantics. Default
            false: flagged, not fatal, per spec §3.7. Never affects ``max_depth``.

    Returns:
        A :class:`TrustPathReport`; ``path_walk_ok`` is the only pass signal.
    """
    if max_depth is not None and max_depth < 0:
        raise TrustPathError("max_depth must be >= 0")
    hops = path.hops
    anchors: dict[str, set[str]] = {}
    anchor_keys: dict[str, bytes] = {}
    for anchor in pinned_anchors:
        anchors.setdefault(anchor.digest, set()).add(anchor.org)
        anchor_keys[anchor.digest] = anchor.spki_der

    acyclic = _path_is_acyclic(path)
    signed_flags = _signed_depth_flags(path)
    policy_flag: tuple[int, ...] = (
        (max_depth,) if max_depth is not None and len(hops) > max_depth else ()
    )
    policy_exceeded = bool(policy_flag)
    depth_flags = tuple(sorted({*signed_flags, *policy_flag}))
    revoked_hits = _revoked_hits(path, frozenset(revoked))
    base: dict[str, Any] = {
        "acyclic": acyclic,
        "delegation_depth_exceeded": bool(depth_flags),
        "depth_flags": depth_flags,
        "signed_depth_flags": signed_flags,
        "policy_depth_exceeded": policy_exceeded,
        "strict_depth": strict_depth,
        "path_touches_revoked": bool(revoked_hits),
        "revoked_hits": revoked_hits,
        "hop_count": len(hops),
    }

    def fail(index: int, reason: str, detail: str, *, terminates: bool) -> TrustPathReport:
        return TrustPathReport(
            path_walk_ok=False,
            terminates_at_anchor=terminates,
            no_broken_hop=False,
            broken_hop=index,
            reason=reason,
            detail=detail,
            anchor_org=hops[0].from_org if terminates else None,
            anchor_digest=hops[0].anchor_digest if terminates else None,
            **base,
        )

    root = hops[0]
    if root.anchor_digest not in anchors:
        return fail(
            0,
            "anchor_not_pinned",
            "hop 0 resolves toward an anchor the verifier did not pin",
            terminates=False,
        )
    if root.from_org not in anchors[root.anchor_digest]:
        return fail(
            0,
            "anchor_org_mismatch",
            f"hop 0 from_org {root.from_org!r} is not the org pinned for that anchor",
            terminates=False,
        )

    signer: _PublicKey = _load_spki(anchor_keys[root.anchor_digest])
    seen_keys = {root.anchor_digest}
    prev_to: str | None = None
    for i, hop in enumerate(hops):
        if hop.anchor_digest != root.anchor_digest:
            return fail(
                i,
                "anchor_mismatch",
                f"hop {i} resolves toward a different anchor than hop 0",
                terminates=False,
            )
        if prev_to is not None and hop.from_org != prev_to:
            return fail(
                i,
                "broken_linkage",
                f"hop {i} from_org {hop.from_org!r} is not hop {i - 1}'s to_org {prev_to!r}",
                terminates=True,
            )
        message = _hop_statement(hop)
        if f"sha256:{hashlib.sha256(message).hexdigest()}" != hop.statement_digest:
            return fail(
                i,
                "statement_digest_mismatch",
                f"hop {i} statement_digest does not match its signed statement",
                terminates=True,
            )
        signature = _strict_b64decode(hop.signature, field="signature", cap=_MAX_SIG_B64)
        if not _verify_signature(signer, signature, message):
            return fail(
                i,
                "bad_signature",
                f"hop {i} signature does not verify under the key "
                + ("pinned for the anchor" if i == 0 else f"hop {i - 1} vouched for"),
                terminates=True,
            )
        try:
            subject_der = _strict_b64decode(
                hop.subject_public_key, field="subject_public_key", cap=_MAX_KEY_B64
            )
            subject = _load_spki(subject_der)
        except TrustPathError as exc:
            return fail(i, "bad_subject_key", f"hop {i}: {exc}", terminates=True)
        subject_digest = key_digest(subject_der)
        if subject_digest in seen_keys:
            return TrustPathReport(
                **{
                    **base,
                    "acyclic": False,
                    "path_walk_ok": False,
                    "terminates_at_anchor": True,
                    "no_broken_hop": False,
                    "broken_hop": i,
                    "reason": "cycle",
                    "detail": f"hop {i} vouches for a key already on the path",
                    "anchor_org": root.from_org,
                    "anchor_digest": root.anchor_digest,
                }
            )
        seen_keys.add(subject_digest)
        signer = subject
        prev_to = hop.to_org

    leaf = hops[-1]
    if not acyclic:
        first_dup = _first_repeat_index(path)
        return fail(
            first_dup,
            "cycle",
            f"hop {first_dup} returns to an org already on the path",
            terminates=True,
        )
    reason: str | None = None
    detail: str | None = None
    violation: int | None = None
    if revoked_hits:
        reason = "path_touches_revoked"
        detail = "the path transits a verifier-supplied revoked subject"
    elif strict_depth and signed_flags:
        # Only reached once every hop's signature verified, so the constraint
        # being enforced is one its issuer really signed.
        violation = _first_signed_violation(path, signed_flags)
        reason = "signed_depth_exceeded"
        detail = (
            f"hop {violation} was issued beyond the signed max_path_length of hop(s) "
            f"{list(signed_flags)} (strict_depth)"
        )
    ok = reason is None
    return TrustPathReport(
        path_walk_ok=ok,
        terminates_at_anchor=True,
        no_broken_hop=True,
        reason=reason,
        detail=detail,
        depth_violation_hop=violation,
        anchor_org=root.from_org,
        anchor_digest=root.anchor_digest,
        leaf_org=leaf.to_org if ok else None,
        leaf_key_digest=key_digest(signer) if ok else None,
        **base,
    )


def _first_repeat_index(path: TrustPath) -> int:
    seen = {path.hops[0].from_org}
    for i, hop in enumerate(path.hops):
        if hop.to_org in seen:
            return i
        seen.add(hop.to_org)
    return 0  # pragma: no cover - only called when a repeat exists


def structural_summary(path: TrustPath) -> list[dict[str, Any]]:
    """Per-hop rows for display — **unverified**; no signature is checked here."""
    return [
        {
            "index": i,
            "from_org": hop.from_org,
            "to_org": hop.to_org,
            "statement_digest": hop.statement_digest,
            "anchor_digest": hop.anchor_digest,
            "max_path_length": hop.max_path_length,
        }
        for i, hop in enumerate(path.hops)
    ]
