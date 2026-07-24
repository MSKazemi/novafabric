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

"""Federated exchange + trust-anchor pin — ADR-0168 D1/D2, P1 (NF-361/NF-362).

Org A holds evidence that org B produced. This module records *that the
exchange happened and on what terms* — the foreign bundle's digest, the
references imported, and which foreign trust bundle A pinned in order to
evaluate B at all. It records nothing about whether B's run was correct, and
it is not a step toward deciding that.

Four invariants from ADR-0168 shape every choice here:

- **I-1 Record-only.** NovaFabric records that *other parties* established
  trust. It is never the CA, PKI root, identity provider, notary,
  transparency-service operator, or trust authority, and nothing in this
  module adjudicates whether a foreign org deserves trust (ADR-0168
  in-mission boundary). ``tests/federation/test_facet.py`` asserts the package
  exports no such entry point.
- **I-2 References and digests only.** Digests, signatures, public trust-bundle
  digests, and URIs cross the org boundary. Raw run payloads, prompts, weights,
  and private keys never do (ADR-0009, ADR-0021 §4).
- **I-3 Fail-open, additive-first.** No federation material ⇒ no facet, never
  an exception. A local-only capsule is returned byte-identical.
- **I-4 Absent is not false.** An unresolvable foreign reference is
  ``unbound``, not *invalid*. An unpinned trust domain is ``unknown``, not
  *untrusted* — and equally not *trusted*.

What a reference import is NOT
------------------------------
``no_shared_backend`` is the load-bearing claim of D1, so it is worth being
blunt about what it costs. A reference import binds a foreign bundle **by
digest**: A holds a 32-byte commitment to bytes it may never have seen, kept
in a store B cannot reach and A does not share. That is the whole point — no
common backend, no shared key, no copy of B's run data.

It follows that a reference import proves *the exchange*, never *the
contents*. So this module offers no way to read one as though the local node
had checked B's bytes:

- a reference resolves to :data:`ReferenceState` — ``bound`` or ``unbound``.
  There is no ``verified`` member, because holding a digest cannot produce one.
- ``verified.refs_resolvable`` records whether the refs could be *located*.
  Locating an artifact is not inspecting it, and the name is deliberately not
  ``refs_verified``.
- no field on any model here can hold bytes; :func:`_reject_payload` rejects
  them at construction, so foreign run data cannot arrive by accident and then
  be mistaken for something this node validated.

No path walk in P1
------------------
D2 pins **one** foreign anchor. The transitive A→B→C path (D2's second half,
NF-363) is P2 and is deliberately absent: there is no ``trust_path`` field, no
hop model, and no walker. This matters more than a missing feature usually
does, because the failure mode is silent — a caller who could ask "is org C
trusted?" of a facet that pins only org B would get an answer composed from
nothing. :func:`anchor_state` therefore answers only about the exact domain
pinned; every other domain is ``unknown``, and ``unknown`` never widens into
an inference about a third org.

Hash construction — no tree
---------------------------
:func:`digest_artifact` is plain ``hashlib.sha256`` over the artifact's bytes.

The repository carries two mutually incompatible Merkle constructions —
``evidence/merkle.py`` (RFC 6962, domain-separated leaf/inner prefixes) and
``trust/novaseal/merkle.py`` (pairwise with odd-duplicate padding) — and mixing
leaves from one with the other's combiner silently yields a wrong root that
still looks like a root. P1 binds *single artifacts by identity* and needs no
inclusion proof over a set, so it needs no tree, which avoids the choice
entirely. ``tests/federation/test_facet.py`` asserts the digest is
bit-identical to raw ``hashlib.sha256`` over the artifact bytes, so a tree
cannot creep in here later unnoticed.

Note this is *not* canonical-JSON hashing either: what is being bound is a
foreign org's bundle as a byte sequence, and re-serialising someone else's
artifact before digesting it would make the binding depend on our serialiser
rather than on their bytes.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_validator

FACET_NAME = "federation"
SCHEMA_VERSION = "0.1.0"

#: The one digest form the rest of the capsule uses. Matched strictly
#: (lower-case hex, exact length) so a truncated or upper-cased digest fails at
#: construction rather than silently failing to match a foreign artifact years
#: later, during an audit of an exchange nobody present still remembers.
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

#: An identifier or reference, never a document. Anything longer is
#: overwhelmingly likely to be foreign run payload smuggled through a ref
#: field — exactly what I-2 exists to stop. Org ids, capsule URIs and trust
#: domains are short; a DSSE signature is the longest legitimate value here,
#: and is still far under this bound.
MAX_REF_LENGTH = 2048

#: Markers of private key material in PEM and OpenSSH encodings. A private key
#: reaching this boundary is the single worst I-2 failure available: it is
#: irreversible the moment it is sealed into a capsule, and unlike a leaked
#: payload it compromises every artifact the key ever signed. Checked as a
#: substring rather than a format parse because the goal is to refuse anything
#: key-shaped, not to correctly decode it.
_PRIVATE_KEY_MARKERS: tuple[str, ...] = (
    "PRIVATE KEY",
    "BEGIN OPENSSH PRIVATE",
    "BEGIN PGP PRIVATE",
)

#: Whether a foreign reference could be located locally.
#:
#: Two members, not three. ``bound`` says a digest names an artifact this node
#: can find; it does **not** say the artifact was opened, parsed, or judged. A
#: third ``verified`` member would be a lie the type system would then help
#: callers tell (see the module docstring).
ReferenceState = Literal["bound", "unbound"]

#: Whether a foreign trust domain appears in this facet's pin.
#:
#: ``unknown`` is not ``untrusted``. NovaFabric records who pinned what; it has
#: no opinion to express about a domain nobody pinned, and reporting one as
#: *untrusted* would be an adjudication (I-1). There is deliberately no
#: ``trusted`` member either: a pin records an act, not a verdict, and a
#: caller must not be able to launder "A pinned B's bundle" into "B is
#: trustworthy" — still less into a claim about some org C (I-4).
AnchorState = Literal["pinned", "unknown"]

#: SPIFFE-federation bundle-endpoint profiles (ADR-0168 D2, NF-362).
#: Closed on purpose: an unrecognised profile would describe an exchange whose
#: authentication properties no later verifier can reconstruct.
EndpointProfile = Literal["https_web", "https_spiffe"]


# ── Errors ────────────────────────────────────────────────────────────────


class FederationError(Exception):
    """Base class for every error this layer raises.

    Subclasses :class:`Exception` rather than :class:`ValueError`: a caller
    wrapping cross-org exchange work wants to catch *federation* failures, and
    inheriting from ValueError would also swallow unrelated coercion errors
    raised from inside the same block.
    """


class InvalidReferenceError(FederationError):
    """Raised when a digest, identifier, or endpoint is malformed."""


class PayloadCrossedBoundaryError(FederationError):
    """Raised when foreign run data or key material arrives where a ref belongs.

    Distinct from :class:`InvalidReferenceError` on purpose: a malformed digest
    is a caller mistake, while a payload or private key reaching this boundary
    is an I-2 violation with a blast radius beyond the current call, and the
    two want very different responses from whoever reads the traceback.
    """


# ── Reference and digest validation ───────────────────────────────────────


def _reject_payload(value: object, *, field: str) -> None:
    """Raise if *value* is payload or key material rather than a reference (I-2)."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        raise PayloadCrossedBoundaryError(
            f"{field} must be a reference (sha256 digest or URI), not raw bytes; "
            "digest the artifact yourself with digest_artifact() and pass the "
            "result (ADR-0168 I-2 — a foreign org's bytes never enter the facet)"
        )
    if isinstance(value, (list, tuple, dict)):
        raise PayloadCrossedBoundaryError(
            f"{field} must be a single reference, not a container; an embedded "
            "structure is foreign payload, not a reference to it (ADR-0168 I-2)"
        )
    if isinstance(value, str):
        for marker in _PRIVATE_KEY_MARKERS:
            if marker in value:
                raise PayloadCrossedBoundaryError(
                    f"{field} contains private key material; a private key never "
                    "leaves the org that holds it, and federation exchanges only "
                    "digests, signatures, public trust bundles and references "
                    "(ADR-0168 I-2, ADR-0021 §4)"
                )


def _validate_digest(value: object, *, field: str) -> str:
    """Return *value* if it is a ``sha256:`` content digest.

    A cross-org binding must be by *content identity*. A URL would not be a
    binding at all — it names where something lives, not what it is, and the
    artifact at a foreign org's URL is precisely the kind of thing that gets
    replaced underneath its own address, by a party this node cannot audit.
    """
    _reject_payload(value, field=field)
    if not isinstance(value, str):
        raise InvalidReferenceError(f"{field} must be a string digest")
    if not _DIGEST_RE.match(value):
        raise InvalidReferenceError(f"{field} must be 'sha256:<64 hex>', got {value!r}")
    return value


def _validate_ref(value: object, *, field: str) -> str:
    """Return *value* if it is an acceptable short reference or identifier.

    Deliberately permissive beyond digest shapes: org identifiers, capsule
    URIs, SPIFFE trust domains and DSSE signature strings all legitimately
    appear here in different encodings, and forcing them into one form would
    push callers to synthesise fake values — worse evidence than the opaque
    reference they actually hold.
    """
    _reject_payload(value, field=field)
    if not isinstance(value, str):
        raise InvalidReferenceError(f"{field} must be a string reference")
    if not value.strip():
        raise InvalidReferenceError(
            f"{field} must be non-empty; an unnamed party or artifact cannot be "
            "reconciled against the foreign org later (NF-361)"
        )
    if len(value) > MAX_REF_LENGTH:
        raise PayloadCrossedBoundaryError(
            f"{field} is {len(value)} chars, over the {MAX_REF_LENGTH}-char "
            "reference limit; this looks like inlined foreign payload, not a ref"
        )
    return value


def _validate_endpoint(value: object, *, field: str) -> str:
    """Return *value* if it is an ``https://`` bundle endpoint.

    Both endpoint profiles NF-362 defines are ``https_*``. An ``http://``
    endpoint could not have delivered an authenticated trust bundle, so
    recording one would preserve a claim about the exchange that was never
    true — and the record, not the fetch, is what an auditor sees years later.
    Resolution itself stays out of NovaFabric's hot path; only the recorded
    URL is checked.
    """
    ref = _validate_ref(value, field=field)
    if not ref.startswith("https://"):
        raise InvalidReferenceError(
            f"{field} must be an https:// URL, got {ref!r}; a trust bundle "
            "fetched without transport authentication was not federated "
            "(ADR-0168 D2, NF-362)"
        )
    return ref


def digest_artifact(content: str | bytes) -> str:
    """Return the ``sha256:`` digest of a foreign artifact's bytes.

    Plain ``hashlib.sha256`` — no Merkle leaf prefix, no tree, no domain
    separation, and no canonical-JSON round trip (see the module docstring for
    why each is wrong here). Emitted in the same ``sha256:<hex>`` form the rest
    of the capsule uses, so a verifier does not have to know which subsystem
    wrote it.

    Offered for the one legitimate local computation in a reference import: an
    importer handed a bundle out-of-band digests it here and compares the
    result against :attr:`ExchangeManifest.bundle_digest`. A match means the
    bytes are the ones the manifest names. It does **not** mean the foreign
    run was correct, and it does not promote the reference past ``bound``.

    The content is hashed and discarded; nothing here retains it (I-2).
    """
    raw = content.encode("utf-8") if isinstance(content, str) else content
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


# ── Objects ───────────────────────────────────────────────────────────────


class ExchangeManifest(BaseModel):
    """A reference import of a foreign org's sealed bundle — NF-361, D1.

    Records that A imported B's bundle by digest and reference. Carries no
    copy of B's run data, and no field on this model can hold any.
    """

    model_config = ConfigDict(extra="allow")

    #: Opaque org identifier or public-key digest — never a company name,
    #: contact, or anything else that would make the exchange record itself a
    #: disclosure (ADR-0009).
    source_org: str
    #: The imported foreign bundle, bound by identity rather than location.
    bundle_digest: str
    #: References to the imported capsules. URIs or digests only.
    import_refs: list[str] = []
    #: The exporting org's DSSE signature over this manifest. Optional: an
    #: exchange that arrived unsigned is a *weaker* exchange, and recording it
    #: as absent is the honest representation. Fabricating or omitting the
    #: manifest entirely would instead hide that the import happened at all.
    exchange_signature: str | None = None
    #: Asserts the exchange used no common store. Required, with no default:
    #: this is D1's load-bearing claim, and a default would let a caller who
    #: never considered their storage topology emit an assertion about it that
    #: they never made.
    no_shared_backend: bool
    #: Digest of the local capsule root this import is bound into. Optional
    #: because at import time the local capsule is often still being written,
    #: and synthesising a root would fabricate a binding.
    bound_root: str | None = None

    @field_validator("source_org", mode="before")
    @classmethod
    def _check_source_org(cls, v: object) -> str:
        # mode="before" throughout: Pydantic's own str coercion would reject
        # bytes first, with a generic "not a valid string" that tells the
        # caller nothing about *why* bytes are forbidden here (I-2).
        return _validate_ref(v, field="source_org")

    @field_validator("bundle_digest", mode="before")
    @classmethod
    def _check_bundle_digest(cls, v: object) -> str:
        return _validate_digest(v, field="bundle_digest")

    @field_validator("bound_root", mode="before")
    @classmethod
    def _check_bound_root(cls, v: object) -> str | None:
        if v is None:
            return None
        return _validate_digest(v, field="bound_root")

    @field_validator("exchange_signature", mode="before")
    @classmethod
    def _check_signature(cls, v: object) -> str | None:
        if v is None:
            return None
        return _validate_ref(v, field="exchange_signature")

    @field_validator("import_refs", mode="before")
    @classmethod
    def _check_import_refs(cls, v: object) -> list[str]:
        if isinstance(v, (bytes, bytearray, memoryview)):
            raise PayloadCrossedBoundaryError(
                "import_refs must be a list of references, not raw bytes "
                "(ADR-0168 I-2)"
            )
        if not isinstance(v, (list, tuple)):
            raise InvalidReferenceError("import_refs must be a list of references")
        return [
            _validate_ref(item, field=f"import_refs[{index}]")
            for index, item in enumerate(v)
        ]


class TrustAnchorPin(BaseModel):
    """A pinned foreign trust bundle — NF-362, D2.

    Records *which* foreign roots A pinned in order to evaluate B, in the
    SPIFFE-federation bundle-endpoint shape. NovaFabric pins by digest and
    never issues, hosts, or vouches for the roots (I-1).

    Note what this object does not have: a parent, a successor, or a delegated
    child. It is a single pin, not a link in a chain — P1 has no path (see the
    module docstring).
    """

    model_config = ConfigDict(extra="allow")

    #: The foreign SPIFFE trust domain the pin is *about*, e.g.
    #: ``spiffe://orgB.example``.
    foreign_trust_domain: str
    #: The URL the bundle was fetched from, recorded for audit. Resolution is
    #: out of NovaFabric's hot path; this is evidence of provenance, not a
    #: live handle.
    bundle_endpoint: str
    endpoint_profile: EndpointProfile
    #: Digest of the pinned foreign roots. The pin *is* this digest: the roots
    #: themselves stay the foreign org's own, and are bound by identity so a
    #: later substitution at the endpoint is detectable.
    trust_bundle_digest: str
    #: When the bundle was acquired. Required: a pin with no time cannot be
    #: placed relative to a later revocation (NF-369, P5), which would make
    #: the withdrawal unassessable against exactly the records it concerns.
    acquired_at: str

    @field_validator("foreign_trust_domain", mode="before")
    @classmethod
    def _check_domain(cls, v: object) -> str:
        return _validate_ref(v, field="foreign_trust_domain")

    @field_validator("bundle_endpoint", mode="before")
    @classmethod
    def _check_endpoint(cls, v: object) -> str:
        return _validate_endpoint(v, field="bundle_endpoint")

    @field_validator("trust_bundle_digest", mode="before")
    @classmethod
    def _check_bundle_digest(cls, v: object) -> str:
        return _validate_digest(v, field="trust_bundle_digest")

    @field_validator("acquired_at")
    @classmethod
    def _check_acquired_at(cls, v: str) -> str:
        if not v.strip():
            raise ValueError(
                "acquired_at must be non-empty; a pin with no time cannot be "
                "ordered against a later trust withdrawal (NF-362)"
            )
        return v


class FederationVerification(BaseModel):
    """What was checked about this facet — tri-state throughout (I-4).

    Every field is ``bool | None``, and ``None`` means *not checked*, never
    *failed*. The distinction is the whole point: an importer that never
    attempted signature verification and one that attempted it and got a bad
    signature have produced very different evidence, and collapsing them into
    ``False`` would make an unchecked exchange indistinguishable from a
    detected forgery — in the direction that cries wolf, until the real
    finding is ignored.
    """

    model_config = ConfigDict(extra="allow")

    #: Did the exporting org's DSSE signature over the manifest verify?
    exchange_sig_ok: bool | None = None
    #: Could the imported refs be *located*? Deliberately not
    #: ``refs_verified``: locating an artifact is not inspecting it, and a
    #: reference import never sees the foreign contents at all.
    refs_resolvable: bool | None = None
    #: Was this facet hashed into the capsule's sealed root?
    sealed_into_root: bool | None = None


class FederationFacet(BaseModel):
    """The optional ``facets.federation`` block — ADR-0168 P1 (I-3).

    Carries at most one exchange manifest and at most one trust-anchor pin.
    Singular on purpose in P1: a *list* of anchors is the shape from which a
    caller starts composing a path, and P1 composes nothing.
    """

    model_config = ConfigDict(extra="allow")

    schema_version: str = SCHEMA_VERSION
    exchange: ExchangeManifest | None = None
    trust_anchor: TrustAnchorPin | None = None
    verified: FederationVerification | None = None


# ── Reading a facet without over-reading it ───────────────────────────────


def reference_state(
    manifest: ExchangeManifest,
    ref: str,
    *,
    resolvable: Iterable[str] = (),
) -> ReferenceState:
    """Report whether *ref* resolves locally — ``bound`` or ``unbound`` (I-4).

    *resolvable* is the set of references this node can currently locate,
    supplied by the caller because locating artifacts is storage's job, not
    this module's.

    A ref that is not in the manifest at all is ``unbound``, not an error: a
    verifier sweeping many manifests for one artifact must be able to ask
    every one of them and collect answers, rather than lose the sweep to the
    first manifest that happens not to mention it.

    ``unbound`` means *this node cannot find it*. It does not mean the foreign
    artifact is missing, forged, or invalid — B's bundle sitting healthily in
    B's store is ``unbound`` here, and that is the normal state of a
    no-shared-backend exchange, not a finding.
    """
    if ref not in manifest.import_refs:
        return "unbound"
    return "bound" if ref in set(resolvable) else "unbound"


def anchor_state(facet: FederationFacet, trust_domain: str) -> AnchorState:
    """Report whether *trust_domain* is the domain this facet pins (I-1, I-4).

    Returns ``pinned`` only for an exact match against the single pinned
    domain. Everything else is ``unknown``.

    Read that second sentence as the security property it is. If this facet
    pins org B, and a caller asks about org C — because B vouched for C, or
    because B and C are in the same consortium, or for any other reason a
    human might find persuasive — the answer is ``unknown``. Establishing
    that A can reach C *via* B requires walking a chain of signed delegation
    statements to a common anchor, which is NF-363/P2 and does not exist yet.
    Until it does, there is no input from which this function could honestly
    return anything else, and returning ``pinned`` on a suffix, subdomain, or
    consortium-membership heuristic would be a transitive-trust conclusion
    drawn from no evidence at all.

    ``pinned`` is likewise not ``trusted``: it reports that someone recorded a
    pin, which is an act, not a verdict on the foreign org (I-1).
    """
    pin = facet.trust_anchor
    if pin is None:
        return "unknown"
    # Exact equality, never prefix or suffix matching. `spiffe://evil-orgB.example`
    # ends with the pinned `orgB.example`, and `spiffe://orgB.example.attacker.tld`
    # begins with it; either heuristic hands an attacker a pin they never earned
    # by choosing a name.
    return "pinned" if pin.foreign_trust_domain == trust_domain else "unknown"


# ── Facet assembly ────────────────────────────────────────────────────────


def build_exchange(
    source_org: str,
    bundle_digest: str,
    *,
    no_shared_backend: bool,
    import_refs: Sequence[str] = (),
    exchange_signature: str | None = None,
    bound_root: str | None = None,
) -> ExchangeManifest:
    """Assemble a reference-import manifest (NF-361).

    ``no_shared_backend`` is keyword-only and has no default, so the claim is
    always made deliberately by a caller who knows their topology.

    *import_refs* is passed to the model uncoerced. Wrapping it in ``list()``
    here would turn a ``bytes`` argument into a list of integers before the
    I-2 validator ever saw it, converting "a foreign bundle was inlined" into
    the far less informative "reference must be a string".
    """
    return ExchangeManifest(
        source_org=source_org,
        bundle_digest=bundle_digest,
        import_refs=import_refs,  # type: ignore[arg-type]
        exchange_signature=exchange_signature,
        no_shared_backend=no_shared_backend,
        bound_root=bound_root,
    )


def build_trust_anchor(
    foreign_trust_domain: str,
    trust_bundle_digest: str,
    *,
    bundle_endpoint: str,
    endpoint_profile: EndpointProfile,
    acquired_at: str,
) -> TrustAnchorPin:
    """Assemble a foreign trust-anchor pin (NF-362)."""
    return TrustAnchorPin(
        foreign_trust_domain=foreign_trust_domain,
        bundle_endpoint=bundle_endpoint,
        endpoint_profile=endpoint_profile,
        trust_bundle_digest=trust_bundle_digest,
        acquired_at=acquired_at,
    )


def build_facet(
    *,
    exchange: ExchangeManifest | None = None,
    trust_anchor: TrustAnchorPin | None = None,
    verified: FederationVerification | None = None,
) -> FederationFacet | None:
    """Assemble the ``federation`` facet, or None when there is nothing to record.

    Returns None when neither an exchange nor a pin is present, rather than an
    empty facet: under I-3 a run that federated nothing must produce no facet
    at all, and an empty facet would assert "this run recorded a cross-org
    exchange with no content" — a claim nobody made.

    A ``verified`` block alone is not enough to produce a facet either. It
    describes checks *on* an exchange or pin; with neither present it would
    describe checks on nothing.
    """
    if exchange is None and trust_anchor is None:
        return None
    return FederationFacet(
        exchange=exchange,
        trust_anchor=trust_anchor,
        verified=verified,
    )


def attach_facet(
    capsule: dict[str, Any], facet: FederationFacet | None
) -> dict[str, Any]:
    """Attach the federation facet to a capsule dict, additively.

    Writes nothing when *facet* is None: a run with no federation material
    must be byte-identical to one captured before this feature existed (I-3).
    Returns a new dict; the input is not mutated.
    """
    if facet is None:
        return capsule
    out = dict(capsule)
    facets = dict(out.get("facets") or {})
    # exclude_none so an absent signature, bound_root, or unchecked
    # verification field is *absent*, not `null`. In a layer whose whole point
    # is that "not recorded" differs from "recorded as false", that
    # distinction has to survive serialisation (I-4).
    facets[FACET_NAME] = facet.model_dump(exclude_none=True)
    out["facets"] = facets
    return out


def facet_from_capsule(capsule: dict[str, Any]) -> FederationFacet | None:
    """Read the federation facet back out of a capsule dict.

    Returns None when the capsule has no facet — the overwhelmingly common
    case, and not an error (I-3).
    """
    facets = capsule.get("facets")
    if not isinstance(facets, dict):
        return None
    block = facets.get(FACET_NAME)
    if not isinstance(block, dict):
        return None
    return FederationFacet.model_validate(block)


def scan_for_payload(values: Sequence[object]) -> None:
    """Raise if any value is payload- or key-shaped rather than a reference (I-2).

    Exposed so a caller assembling a manifest from an untrusted foreign
    exchange can check before constructing, rather than discovering the
    problem as a validation error halfway through a model.
    """
    for index, value in enumerate(values):
        _validate_ref(value, field=f"reference[{index}]")
