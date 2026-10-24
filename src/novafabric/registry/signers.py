"""Maker-checker signer trust list and signature verification (ADR-0312).

ADR-0058 stored a proposer and an approver signature with every registry promotion but
never verified either, and bound no key to any identity: a self-declared name plus a key
minted on the fly was enough to "approve". This module closes that:

* an operator-held **signer trust list** binds each Ed25519 public key (by its keyring
  fingerprint) to exactly one identity and to the roles it may exercise
  (``proposer`` / ``approver``);
* every proposal and approval signature is **verified** over a canonical payload with the
  public key the trust list binds to the claimed identity;
* separation of duties compares the **bound** identities, not the names a caller typed.

Trust list file (YAML), resolved by :func:`novafabric._paths.promote_signers_path`
(``NOVAFABRIC_PROMOTE_SIGNERS`` → ``$NOVAFABRIC_HOME/promote-signers.yaml``)::

    signers:
      - identity: alice
        public_key: <base64 of the raw 32-byte Ed25519 public key>
        fingerprint: 1a2b3c4d5e6f7a8b      # optional cross-check
        roles: [proposer]
      - identity: bob
        public_key: <base64>
        roles: [approver]

``nova promote keygen --identity NAME`` prints a ready-made entry. The list is a trust
anchor: whoever can write it decides who may approve. Keep it operator-owned and
read-only to signers (ADR-0312 "Limits").
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from novafabric._paths import promote_signers_path
from novafabric.trust.keyring import fingerprint_of, verify_sig

try:
    import yaml
except ImportError:  # pragma: no cover - PyYAML is a runtime dependency
    yaml = None  # type: ignore[assignment]

#: Recorded in ``promotion_proposals.signoff_scheme`` for rows written under ADR-0312.
SIGNOFF_SCHEME = "nova-promote-signoff/v2"

Role = Literal["proposer", "approver"]
Party = Literal["proposer", "approver"]
_ROLES: frozenset[str] = frozenset({"proposer", "approver"})
_ENTRY_KEYS: frozenset[str] = frozenset({"identity", "public_key", "fingerprint", "roles"})
_MAX_SIGNERS = 1024
_MAX_IDENTITY_LEN = 256


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class SignerVerificationError(Exception):
    """A maker-checker signature could not be verified against the trust list.

    ``party`` names which side failed (``"proposer"`` or ``"approver"``) when known; the CLI
    maps it to exit 3 (proposal side) or 4 (approval side).
    """

    def __init__(self, message: str, *, party: Party | None = None) -> None:
        super().__init__(message)
        self.party: Party | None = party


class SignerTrustListError(SignerVerificationError):
    """The signer trust list is missing, unreadable or malformed."""


class UntrustedSignerError(SignerVerificationError):
    """The signing key is not in the trust list, or is not trusted for this role."""


class SignerIdentityMismatchError(SignerVerificationError):
    """The signing key is bound to a different identity than the one claimed."""


class InvalidSignatureError(SignerVerificationError):
    """The signature does not verify over the canonical payload."""


class UnverifiedProposalError(SignerVerificationError):
    """The open proposal predates ADR-0312 and carries no verifiable signature."""


# ---------------------------------------------------------------------------
# Canonical payloads
# ---------------------------------------------------------------------------


def _canonical(doc: dict[str, str]) -> bytes:
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def proposal_payload(
    *, proposal_id: str, name: str, version: str, to_status: str, spec_sha256: str
) -> bytes:
    """Bytes the maker signs: scheme, step, proposal id, asset ref, its content and stage.

    Review replay-lineage-registry-04: v1 signed ``name@version`` only, so unregistering
    and re-registering different content under the same version between propose and
    approve promoted content nobody reviewed. *spec_sha256* (``spec_digest`` of the
    asset's ``spec_json``) binds the signature to the reviewed content; the scheme is
    v2 so a v1 signature can never verify.
    """
    return _canonical(
        {
            "scheme": SIGNOFF_SCHEME,
            "step": "propose",
            "proposal_id": proposal_id,
            "asset": f"{name}@{version}",
            "spec_sha256": spec_sha256,
            "to_status": to_status,
        }
    )


def approval_payload(
    *,
    proposal_id: str,
    name: str,
    version: str,
    to_status: str,
    proposer_sig: str,
    spec_sha256: str,
) -> bytes:
    """Bytes the checker signs: the proposal fields plus a hash of the maker's signature.

    Binding the proposer signature makes an approval valid for exactly one signed proposal;
    the distinct ``step`` stops a proposal signature being replayed as an approval.
    """
    return _canonical(
        {
            "scheme": SIGNOFF_SCHEME,
            "step": "approve",
            "proposal_id": proposal_id,
            "asset": f"{name}@{version}",
            "spec_sha256": spec_sha256,
            "to_status": to_status,
            "proposer_sig_sha256": hashlib.sha256(proposer_sig.encode("utf-8")).hexdigest(),
        }
    )


# ---------------------------------------------------------------------------
# Trust list
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TrustedSigner:
    """One trust-list entry: a public key bound to one identity and its roles."""

    identity: str
    fingerprint: str
    public_key: Ed25519PublicKey
    roles: frozenset[str]


@dataclass(frozen=True)
class SignerTrustList:
    """Fingerprint-indexed signer bindings loaded from the operator's trust list."""

    source: str
    by_fingerprint: dict[str, TrustedSigner]

    def verify(
        self,
        *,
        claimed_identity: str,
        key_fp: str,
        role: Role,
        payload: bytes,
        signature: str,
        party: Party,
    ) -> TrustedSigner:
        """Return the bound signer when every check passes; raise otherwise.

        Checks, in order: the fingerprint is listed; it is bound to *claimed_identity*;
        that identity holds *role*; the signature verifies with the bound public key.
        """
        signer = self.by_fingerprint.get(key_fp)
        if signer is None:
            raise UntrustedSignerError(
                f"{party} key {key_fp!r} (identity {claimed_identity!r}) is not in the "
                f"signer trust list ({self.source}). An operator must bind it: run "
                f"`nova promote keygen --identity {claimed_identity}` and add the printed "
                "entry to the trust list.",
                party=party,
            )
        if signer.identity != claimed_identity:
            raise SignerIdentityMismatchError(
                f"{party} key {key_fp!r} is bound to identity {signer.identity!r} in the "
                f"signer trust list, not to {claimed_identity!r}.",
                party=party,
            )
        if role not in signer.roles:
            raise UntrustedSignerError(
                f"identity {signer.identity!r} is not trusted as a {role} "
                f"(trust-list roles: {sorted(signer.roles)}).",
                party=party,
            )
        if not verify_sig(signer.public_key, signature, payload):
            raise InvalidSignatureError(
                f"{party} signature by {signer.identity!r} (key {key_fp}) does not verify "
                "over the canonical promotion payload — the proposal or approval was "
                "altered, or was signed for a different proposal.",
                party=party,
            )
        return signer


def _decode_public_key(value: object, where: str) -> Ed25519PublicKey:
    if not isinstance(value, str) or not value.strip():
        raise SignerTrustListError(f"{where}: public_key must be a non-empty base64 string")
    # Standard or URL-safe alphabet, padding optional; anything else is rejected
    # (validate=True), never silently skipped.
    text = value.strip().replace("-", "+").replace("_", "/")
    try:
        raw: bytes | None = base64.b64decode(text + "=" * (-len(text) % 4), validate=True)
    except (binascii.Error, ValueError):
        raw = None
    if raw is None or len(raw) != 32:
        raise SignerTrustListError(
            f"{where}: public_key must be base64 of a raw 32-byte Ed25519 public key"
        )
    return Ed25519PublicKey.from_public_bytes(raw)


def _parse_entry(entry: object, index: int) -> TrustedSigner:
    where = f"signers[{index}]"
    if not isinstance(entry, dict):
        raise SignerTrustListError(f"{where} must be a mapping")
    unknown = set(entry) - _ENTRY_KEYS
    if unknown:
        raise SignerTrustListError(f"{where}: unknown key(s) {sorted(map(str, unknown))}")
    identity = entry.get("identity")
    if not isinstance(identity, str) or not identity.strip():
        raise SignerTrustListError(f"{where}: identity must be a non-empty string")
    if len(identity) > _MAX_IDENTITY_LEN or identity != identity.strip():
        raise SignerTrustListError(f"{where}: identity is too long or has edge whitespace")
    public_key = _decode_public_key(entry.get("public_key"), where)
    fingerprint = fingerprint_of(public_key)
    declared = entry.get("fingerprint")
    if declared is not None and str(declared) != fingerprint:
        raise SignerTrustListError(
            f"{where}: fingerprint {declared!r} does not match public_key ({fingerprint})"
        )
    roles = entry.get("roles")
    if (
        not isinstance(roles, list)
        or not roles
        or not all(isinstance(r, str) and r in _ROLES for r in roles)
    ):
        raise SignerTrustListError(
            f"{where}: roles must be a non-empty list drawn from {sorted(_ROLES)}"
        )
    return TrustedSigner(
        identity=identity, fingerprint=fingerprint, public_key=public_key,
        roles=frozenset(roles),
    )


def parse_signer_trust_list(raw: Any, *, source: str) -> SignerTrustList:
    """Validate a parsed trust-list document. Strict: any defect is a hard error.

    One key may be bound to exactly one identity — a fingerprint listed twice is refused,
    because it would let one key-holder act under two names.
    """
    if not isinstance(raw, dict) or not isinstance(raw.get("signers"), list):
        raise SignerTrustListError(f"{source}: expected a mapping with a 'signers' list")
    entries = raw["signers"]
    if len(entries) > _MAX_SIGNERS:
        raise SignerTrustListError(f"{source}: more than {_MAX_SIGNERS} signers")
    by_fp: dict[str, TrustedSigner] = {}
    for index, entry in enumerate(entries):
        signer = _parse_entry(entry, index)
        if signer.fingerprint in by_fp:
            raise SignerTrustListError(
                f"{source}: key {signer.fingerprint} is listed more than once "
                f"(identities {by_fp[signer.fingerprint].identity!r} and "
                f"{signer.identity!r}); a key may be bound to one identity only"
            )
        by_fp[signer.fingerprint] = signer
    return SignerTrustList(source=source, by_fingerprint=by_fp)


def load_signer_trust_list(path: Path | None = None) -> SignerTrustList:
    """Load the trust list from *path* (default: :func:`promote_signers_path`).

    Raises:
        SignerTrustListError: the file is absent, unreadable or malformed. An absent list
            is never read as "trust everyone" — maker-checker promotion is refused.
    """
    resolved = path or promote_signers_path()
    if not resolved.is_file():
        raise SignerTrustListError(
            f"No maker-checker signer trust list at {resolved}. Create it (or point "
            "NOVAFABRIC_PROMOTE_SIGNERS at one) listing each signer's identity, public key "
            "and roles; `nova promote keygen --identity NAME` prints an entry (ADR-0312)."
        )
    if yaml is None:  # pragma: no cover
        raise SignerTrustListError("PyYAML is required to read the signer trust list")
    try:
        raw = yaml.safe_load(resolved.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise SignerTrustListError(f"cannot read signer trust list {resolved}: {exc}") from exc
    return parse_signer_trust_list(raw, source=str(resolved))
