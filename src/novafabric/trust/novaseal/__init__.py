"""NovaSeal v0.1 — Cryptographic signing core for NovaFabric capsules.

Implements ADR-0041 (local-key mode only in v0.1):
- DSSE envelope with ECDSA P-256 / SHA-256 (ADR-001), DSSE v1 PAE
- RFC 3161 trusted timestamps from an operator-chosen TSA — opt-in, no default
  TSA and no network call unless ``tsa_url`` is set (ADR-0030, ADR-0292)
- SQLite-backed append-only Merkle log (ADR-003)

Usage:

    from novafabric.trust.novaseal import NovaSeal, KeyConfig

    config = KeyConfig(
        profile="local",
        key_path="/path/to/key.pem",
        cert_path="/path/to/cert.pem",
    )
    seal = NovaSeal(
        config=config,
        tsa_url="https://tsa.example.org/tsr",  # "" (the default) = no timestamp
        db_path="/path/to/merkle.db",
    )
    bundle = seal.seal(capsule_manifest)
    result = seal.verify(capsule_id, seal_dir)
    assert result.signature_ok and result.timestamp_ok is not False and result.log_integrity_ok
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeGuard

from novafabric.trust.novaseal.envelope import (
    EnvelopeError,
    SigningIntent,
    bind_intent,
    create_envelope,
    extract_intent,  # noqa: F401  (re-exported; returns the signed intent only, ADR-0309)
    extract_payload,
    read_intent,
    verify_envelope_encoding,
)
from novafabric.trust.novaseal.merkle import (  # noqa: F401
    ROOT_VERSION_V1,
    MerkleError,
    MerkleLog,
    PostgresMerkleLog,
    UnsupportedRootVersionError,
    _leaf_hash,
    inclusion_proof_length,
    open_merkle_log,
    root_version_of,
    verify_inclusion_proof,
)
from novafabric.trust.novaseal.nonce_store import NonceStore
from novafabric.trust.novaseal.timestamp import (
    TSAUnavailableError,
    check_timestamp,
    request_timestamp,
    verify_timestamp,
)
from novafabric.trust.novaseal.trust_chain import CertChainResult, verify_tsa_cert_chain

if TYPE_CHECKING:  # pragma: no cover — typing only
    from novafabric.trust.novaseal.signing_backend import SigningBackend

log = logging.getLogger(__name__)

# Emit the "no timestamp" note once per process, not once per sealed capsule.
_NO_TSA_NOTE_EMITTED = False


def _note_sealing_without_timestamp() -> None:
    """Say — once per process — that seals carry no RFC 3161 timestamp (ADR-0292)."""
    global _NO_TSA_NOTE_EMITTED
    if _NO_TSA_NOTE_EMITTED:
        return
    _NO_TSA_NOTE_EMITTED = True
    log.warning(
        "NovaSeal: sealing without an RFC 3161 timestamp — no tsa_url is configured "
        "(timestamping is opt-in since v0.103, ADR-0292). The signature and Merkle log "
        "entry are unaffected; set tsa_url in novaseal.yaml to add a trusted time."
    )

#: Merkle inclusion established against the verifier's local (sealer's) log.
LOG_INCLUSION_LOCAL = "local-log"
#: Merkle inclusion established from the proof carried in ``log-entry.json``.
LOG_INCLUSION_PROOF = "carried-proof"
#: No proof carried and the entry is not in any available log — not checked.
LOG_INCLUSION_NOT_CHECKED = "not-checked"
#: The log metadata is inconsistent with the capsule or with the log.
LOG_INCLUSION_FAILED = "failed"

# ``VerificationResult.sealer_log`` — what the verifier's Merkle log(s) said about
# this capsule's entry (ADR-0297 Amendment 2).
#: No log was available to the verifier (an auditor's machine).
SEALER_LOG_NO_LOG = "no-log"
#: A supplied log holds the entry (``log_inclusion`` is then ``local-log`` or ``failed``).
SEALER_LOG_ENTRY_FOUND = "entry-found"
#: The supplied log(s) hold no seals by this signer: another sealer's log or a fresh one.
SEALER_LOG_NOT_SEALERS = "not-sealer-log"
#: The signer's log is available only under a *newer* root version than the record
#: (a v1 capsule on a host that started a v2 log, ADR-0313) and no log of the
#: record's version was supplied (``previous_merkle_dbs``). The record's
#: ``root_version`` is written by whoever wrote ``log-entry.json``, so absence
#: from the newer log cannot be told apart from a forged v1 record: *unverified*,
#: not passed. ``nova verify --strict`` fails on it.
SEALER_LOG_OTHER_VERSION = "unverifiable-other-version"
#: The sealer's log does not hold the entry (``log_inclusion`` is ``failed``).
SEALER_LOG_MISSING = "missing"
#: Inclusion failed on the capsule's own log metadata before any log was consulted.
SEALER_LOG_NOT_CONSULTED = "not-consulted"

# ``VerificationResult.timestamp_trust`` — how far the RFC 3161 *time* is trusted.
#: The token verified and its TSA chain reached a verifier-supplied TSA anchor.
TIMESTAMP_TRUST_ANCHORED = "anchored"
#: The token is intact and bound to this envelope, but no TSA anchor was consulted:
#: its genTime is a claim of whoever holds the embedded TSA key — a self-made,
#: backdated "TSA" reaches this state. Not trusted time.
TIMESTAMP_TRUST_UNANCHORED = "unanchored"
#: No RFC 3161 token (timestamping is opt-in, ADR-0292): no time is claimed.
TIMESTAMP_TRUST_ABSENT = "absent"
#: A token is present and failed integrity, or failed the anchored chain check.
TIMESTAMP_TRUST_INVALID = "invalid"

__all__ = [
    "SealBundle",
    "VerificationResult",
    "KeyConfig",
    "RotationReceipt",
    "NovaSeal",
    "SealError",
    "SigningIntent",
    "NonceStore",
    "CertChainResult",
    "verify_tsa_cert_chain",
    "TSAUnavailableError",
    "request_timestamp",
    "verify_timestamp",
    "verify_seal_dir",
    "LOG_INCLUSION_LOCAL",
    "LOG_INCLUSION_PROOF",
    "LOG_INCLUSION_NOT_CHECKED",
    "LOG_INCLUSION_FAILED",
    "SEALER_LOG_NO_LOG",
    "SEALER_LOG_ENTRY_FOUND",
    "SEALER_LOG_NOT_SEALERS",
    "SEALER_LOG_OTHER_VERSION",
    "SEALER_LOG_MISSING",
    "SEALER_LOG_NOT_CONSULTED",
    "TIMESTAMP_TRUST_ANCHORED",
    "TIMESTAMP_TRUST_UNANCHORED",
    "TIMESTAMP_TRUST_ABSENT",
    "TIMESTAMP_TRUST_INVALID",
    "timestamp_trust_for",
    "unanchored_claims",
    "verify_with_profile",
]


@dataclass
class SealBundle:
    dsse_envelope: bytes    # DSSE JSON, UTF-8
    tsr: bytes              # RFC 3161 TSR, DER-encoded (may be empty if TSA skipped)
    log_entry: dict[str, object]  # Merkle log entry dict
    capsule_id: str         # SHA-256 hex of capsule manifest bytes


@dataclass
class VerificationResult:
    valid: bool
    signature_ok: bool
    # ADR-0301: ``None`` when the capsule carries no RFC 3161 token (absent, not
    # passed); ``True`` only for a present token that verified; ``False`` for a
    # present token that failed. Absence does not invalidate (timestamping is opt-in).
    timestamp_ok: bool | None
    log_integrity_ok: bool
    errors: list[str] = field(default_factory=list)  # noqa: RUF009
    signing_intent: SigningIntent | None = None
    # ADR-0309: "signed" (intent bound inside the signed payload), "label-only" (a seal
    # made before ADR-0309 — the label is unauthenticated), "mismatch" (the unsigned
    # label was edited after signing; the seal fails) or "absent".
    signing_intent_status: str = "absent"
    # True only when a verifier-supplied CA bundle validated the signer chain
    # (``nova verify --ca-bundle``); ``verify_seal_dir`` alone never sets it.
    ca_chain_ok: bool = False
    ca_chain_errors: list[str] = field(default_factory=list)  # noqa: RUF009
    # Whether an RFC 3161 token was present (non-empty ``manifest.dsse.tsr``). Until
    # ADR-0301 ``timestamp_ok`` was True for an absent token too, which printed
    # ``timestamp_ok=True`` for a capsule that carries no timestamp.
    timestamp_present: bool = False
    # Which DSSE PAE the signature verified over: ``"dsse-v1"`` (spec; verifiable
    # with stock DSSE tooling) or ``"legacy-le64"`` (sealed through v0.102.x;
    # verifiable with NovaFabric only). None when the signature did not verify.
    pae_encoding: str | None = None
    # True when a present token passed the strict checks (positional imprint,
    # messageDigest == H(TSTInfo), CMS signature, ESSCertID). False with
    # ``timestamp_ok`` True means only the legacy structural check could run.
    timestamp_strict: bool = False
    # How Merkle inclusion was established — see ``verify_seal_dir``. One of
    # LOG_INCLUSION_LOCAL / _PROOF / _NOT_CHECKED / _FAILED. ``log_integrity_ok``
    # is True only for the first two; ``not-checked`` does not invalidate.
    log_inclusion: str = "failed"
    log_notes: list[str] = field(default_factory=list)  # noqa: RUF009
    # ADR-0301: what the verifier established about the signer — one of
    # ``identity_trust.IDENTITY_TRUST_LEVELS``. ``verify_seal_dir`` consults no CA
    # bundle, so it reports ``self-asserted`` or ``none``; ``nova verify`` raises it
    # to ``local-ca-pinned`` / ``ca-anchored`` when ``--ca-bundle`` validates the chain.
    identity_trust: str = "none"
    # Subject of the embedded signer certificate — an *unverified* claim of the
    # envelope unless ``identity_trust`` is an anchored level.
    signer_subject: str | None = None
    # True when the signer certificate is a ``nova seal init`` local identity.
    local_seal_identity: bool = False
    # How far the RFC 3161 *time* is trusted — one of the ``TIMESTAMP_TRUST_*``
    # values. ``timestamp_ok`` says the token is intact and bound to the envelope;
    # only ``anchored`` says a TSA the verifier trusts vouches for the time.
    timestamp_trust: str = TIMESTAMP_TRUST_ABSENT
    # The token's claimed genTime (ISO 8601) and TSA certificate subject — the
    # token's own, unverified claims unless ``timestamp_trust`` is ``anchored``.
    timestamp_gen_time: str | None = None
    tsa_subject: str | None = None
    # What the verifier's Merkle log(s) said — one of the ``SEALER_LOG_*`` values.
    sealer_log: str = SEALER_LOG_NO_LOG

    def __str__(self) -> str:
        parts = [
            f"signature_ok={self.signature_ok}",
            f"timestamp_ok={self.timestamp_ok}",
            f"timestamp_trust={self.timestamp_trust}",
            f"log_integrity_ok={self.log_integrity_ok}",
            f"log_inclusion={self.log_inclusion}",
            f"sealer_log={self.sealer_log}",
            f"ca_chain_ok={self.ca_chain_ok}",
            f"identity_trust={self.identity_trust}",
        ]
        if self.signing_intent is not None:
            parts.append(f"intent={self.signing_intent.value}")
        return ", ".join(parts)


@dataclass
class KeyConfig:
    profile: str        # "local" for v0.1
    key_path: str       # path to ECDSA P-256 PEM private key
    cert_path: str      # path to self-signed or CA-signed cert


@dataclass
class RotationReceipt:
    old_key_fingerprint: str
    new_key_fingerprint: str
    rotation_log_entry: dict[str, object]


class SealError(Exception):
    """Raised on NovaSeal signing or verification failures."""


# ---------------------------------------------------------------------------
# NovaSeal
# ---------------------------------------------------------------------------

class NovaSeal:
    """Sign, timestamp, and log NovaFabric capsules.

    Args:
        config:    KeyConfig with profile and key/cert paths.
        tsa_url:   RFC 3161 TSA URL; "" disables timestamping (no network call).
        db_path:   Merkle log location: a SQLite path (``~`` is expanded) or a
                   Postgres DSN (never expanded); see ``log_location``.
        tsa_urls:  Optional ordered fallback list of TSA URLs (REG-ADR-007).
                   When it has more than one entry, each is tried in order
                   until one succeeds. Omit (or pass a single-entry list) for
                   the original single-TSA behavior.
        backend:   Optional ``SigningBackend``. Required for the cloud profiles
                   (``aws_kms``, ``azure_kv``, ``gcp_kms``), which have no local
                   private key to load. Build one with
                   ``novafabric.trust.novaseal.config.build_signing_backend()``.
                   Omit for the local profile.
    """

    def __init__(
        self,
        config: KeyConfig,
        tsa_url: str,
        db_path: str,
        tsa_urls: list[str] | None = None,
        backend: "SigningBackend | None" = None,
    ) -> None:
        self._config = config
        self._backend = backend
        # A cloud profile (aws_kms / azure_kv / gcp_kms) has no local private key.
        # ``config.key_path`` is stringified by callers, so an absent path arrives
        # as the literal "None"; treat that as unset rather than as a filename.
        # ``~`` is expanded for library callers too (audit L28); the config
        # loader already expands its own paths.
        self._key_path = (
            Path(config.key_path).expanduser()
            if config.key_path and config.key_path != "None"
            else Path()
        )
        self._cert_path = Path(config.cert_path).expanduser()
        # ADR-0292: timestamping is opt-in. An empty tsa_url with a non-empty
        # tsa_urls list still opts in (the list names the TSAs explicitly).
        self._tsa_url = tsa_url or (tsa_urls[0] if tsa_urls else "")
        self._tsa_urls = tsa_urls
        self._merkle = open_merkle_log(db_path)

    def seal(
        self,
        capsule_manifest: dict[str, object],
        intent: SigningIntent | None = SigningIntent.AUTHORED,
    ) -> SealBundle:
        """Sign, timestamp, and log the capsule.

        Args:
            capsule_manifest: Capsule manifest as a Python dict (will be
                              JSON-serialised to produce the signed payload).
            intent:           Signing intent per FDA 21 CFR §11.50(a)(3).
                              Defaults to AUTHORED; pass None to omit.

        Returns:
            SealBundle with dsse_envelope, tsr, log_entry, and capsule_id.

        Raises:
            SealError: if signing or log-append fails.
        """
        # ADR-0309: the intent is signed inside the payload, not only labelled.
        if intent is not None:
            capsule_manifest = bind_intent(dict(capsule_manifest), intent)
        payload = json.dumps(
            capsule_manifest, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        capsule_id = hashlib.sha256(payload).hexdigest()

        # 1. Create DSSE envelope
        try:
            dsse_bytes = create_envelope(
                payload,
                self._key_path,
                self._cert_path,
                intent=intent,
                backend=self._backend,
            )
        except EnvelopeError as exc:
            raise SealError(f"DSSE signing failed: {exc}") from exc

        # 2. RFC 3161 timestamp — opt-in (ADR-0292); best-effort when configured:
        #    warns but does not fail if the TSA is down.
        tsr_bytes = b""
        if not self._tsa_url:
            _note_sealing_without_timestamp()
        else:
            try:
                tsr_bytes = request_timestamp(
                    dsse_bytes, self._tsa_url, tsa_urls=self._tsa_urls
                )
            except TSAUnavailableError as exc:
                log.warning("TSA unavailable — capsule will be sealed without timestamp: %s", exc)
            except Exception as exc:
                log.warning("Timestamp request failed — continuing without TSR: %s", exc)

        # 3. Append to Merkle log
        entry: dict[str, object] = {
            "capsule_id": capsule_id,
            "keyid": _keyid_from_cert(self._cert_path),
            "has_tsr": bool(tsr_bytes),
        }
        try:
            log_entry = self._merkle.append(entry)
        except MerkleError as exc:
            raise SealError(f"Merkle log append failed: {exc}") from exc

        return SealBundle(
            dsse_envelope=dsse_bytes,
            tsr=tsr_bytes,
            log_entry=log_entry,
            capsule_id=capsule_id,
        )

    def verify(
        self,
        capsule_id: str,
        seal_dir: str,
        *,
        previous_logs: "Sequence[MerkleLog | PostgresMerkleLog]" = (),
        tsa_anchors_pem: bytes | None = None,
    ) -> VerificationResult:
        """Verify signature, TSR, and Merkle inclusion for a capsule.

        Delegates to :func:`verify_seal_dir` with this instance's Merkle log, so
        the sealer's own log is consulted when it holds the entry.

        Args:
            capsule_id:      Unused; kept for API compatibility. The capsule id is
                             re-derived from the signed payload (ADR-0251 §2).
            seal_dir:        Path to the .seal/ directory inside the capsule dir.
            previous_logs:   This host's earlier logs (ADR-0313 migration); see
                             :func:`verify_seal_dir`.
            tsa_anchors_pem: Operator TSA trust anchors (PEM); see
                             :func:`verify_seal_dir`.
        """
        del capsule_id
        return verify_seal_dir(
            Path(seal_dir),
            merkle_log=self._merkle,
            previous_logs=previous_logs,
            tsa_anchors_pem=tsa_anchors_pem,
        )

    def rotate_key(self, new_key_config: KeyConfig) -> RotationReceipt:
        """Append a key-rotation log entry; future seals use the new key."""
        old_fingerprint = _keyid_from_cert(self._cert_path)
        new_fingerprint = _keyid_from_cert(Path(new_key_config.cert_path))

        rotation_entry: dict[str, object] = {
            "event": "key_rotation",
            "old_keyid": old_fingerprint,
            "new_keyid": new_fingerprint,
        }
        log_entry = self._merkle.append(rotation_entry)

        # Switch to new key
        self._config = new_key_config
        self._key_path = Path(new_key_config.key_path)
        self._cert_path = Path(new_key_config.cert_path)

        return RotationReceipt(
            old_key_fingerprint=old_fingerprint,
            new_key_fingerprint=new_fingerprint,
            rotation_log_entry=log_entry,
        )


# ---------------------------------------------------------------------------
# Verification — self-contained; the sealer's log is optional
# ---------------------------------------------------------------------------


#: Size bounds for the files ``verify_seal_dir`` reads from ``.seal/``. The
#: envelope bound matches ``capsule_verify``; a log entry with its inclusion
#: proof is a few KiB, so 4 MiB is far above any real one.
_SEAL_ENVELOPE_MAX_BYTES = 16 * 1024 * 1024
_SEAL_LOG_ENTRY_MAX_BYTES = 4 * 1024 * 1024
_SEAL_READ_OPERATION = "NovaSeal verification"


@dataclass(frozen=True)
class _SealMember:
    """One ``.seal/`` file as read for verification.

    ``data`` is None when the file is absent; ``refused`` names why a present
    entry was not read (a symlink on its path, a special file, over the size
    bound) — such a file is a verification failure, never followed.
    """

    path: Path
    data: bytes | None = None
    refused: str | None = None


def _read_seal_member(seal_path: Path, name: str, *, max_bytes: int) -> _SealMember:
    """Read ``<seal_path>/<name>`` through ``_capsule_fs``: no link, no FIFO, bounded.

    The seal directory itself is opened relative to its parent, so a ``.seal``
    that is itself a symlink is refused too.
    """
    from novafabric._capsule_fs import (  # noqa: PLC0415
        CapsuleEntryError,
        read_capsule_file,
    )

    path = seal_path / name
    root, rel = (
        (seal_path.parent, f"{seal_path.name}/{name}") if seal_path.name else (seal_path, name)
    )
    try:
        data = read_capsule_file(
            root, rel, operation=_SEAL_READ_OPERATION, max_bytes=max_bytes
        )
    except (FileNotFoundError, NotADirectoryError):
        return _SealMember(path)
    except CapsuleEntryError as exc:
        return _SealMember(path, refused=str(exc))
    return _SealMember(path, data=data)


def verify_seal_dir(
    seal_dir: Path | str,
    merkle_log: "MerkleLog | PostgresMerkleLog | None" = None,
    *,
    previous_logs: "Sequence[MerkleLog | PostgresMerkleLog]" = (),
    tsa_anchors_pem: bytes | None = None,
) -> VerificationResult:
    """Verify a capsule's ``.seal/`` directory from the capsule alone.

    Signature and timestamp need nothing but the files. Merkle inclusion is
    checked by every means available and reported in ``log_inclusion``:

    * ``carried-proof`` — ``log-entry.json`` carries ``inclusion_proof`` (sealed
      from v0.103 on) and it recomputes the recorded ``root_hash``;
    * ``local-log`` — *merkle_log* or one of *previous_logs* holds this exact
      leaf at the recorded index, and the recorded tree head (size and root,
      under the record's root version) is one that log's leaves reproduce;
    * ``not-checked`` — neither is available (a v0.102.x seal verified away from
      the sealer's log). The capsule still verifies; the caller must say so;
    * ``failed`` — anything inconsistent: the entry does not hash to its
      ``leaf_hash``, names another capsule, carries a proof that does not verify,
      sits at a different index in a supplied log, records a ``root_hash``
      without its ``tree_size``, records a tree head (size + root) that the supplied log's
      leaves do not reproduce, or is **absent from the sealer's log**.

    *The sealer's log* is a supplied log that holds seals by the same signer
    (entry ``keyid`` or the fingerprint of the certificate whose signature
    verified), whatever its root version. ``sealer_log`` reports what the logs
    said (``SEALER_LOG_*``):

    * the sealer's log of the record's root version lacks the entry → ``failed``
      / ``missing``. A carried proof does not override that absence: the sealer's
      log is the authority on what that sealer sealed;
    * the record claims a root version *newer* than every sealer's log (a v2
      record against a v1 log) → ``failed`` / ``missing``: v2 logs came later, so
      no v1 log produced a v2 seal (ADR-0313);
    * the sealer's log exists only under a *newer* version than the record (a v1
      record on a host that started a v2 log) and no log of the record's version
      was supplied → the carried-proof / not-checked result stands, but
      ``sealer_log`` is ``unverifiable-other-version``. ``root_version`` is read
      from ``log-entry.json``, which the capsule's writer controls, so this
      absence is consistent both with a genuine pre-migration capsule and with a
      key holder's forgery claiming v1. Pass the host's earlier logs as
      *previous_logs* (``previous_merkle_dbs`` in ``novaseal.yaml``) to decide
      it; ``nova verify --strict`` fails on it;
    * no supplied log holds this signer's seals (another sealer's log, a fresh
      log) → the carried-proof / not-checked result stands, ``not-sealer-log``.

    A leaf may occur more than once in a log (the same manifest sealed twice
    with the same key appends a byte-identical leaf); the check asks whether the
    *recorded* index holds the leaf, so every copy verifies.

    Root version (ADR-0313): ``log-entry.json`` may carry ``root_version``
    (absent: v1, every seal written before it). A v1 root does not commit to the
    tree size, so a v1 carried proof cannot tell size n from a log whose leaf n
    duplicates leaf n - 1; ``carried-proof`` notes say so, and only the sealer's
    log (``local-log``) can refuse a wrong ``tree_size``. A v2 proof is bound to
    its ``tree_size`` by the RFC 9162 algorithm.

    A carried proof binds the entry to the tree head recorded at seal time. That
    head is *not* independently anchored here (it is not signed or witnessed), so
    ``carried-proof`` proves the capsule's log metadata is self-consistent, not
    that a third party saw the log; checking a published or witnessed root is the
    job of ``nova seal log verify`` / ADR-0097.

    Timestamp: ``timestamp_ok`` says a present token is intact and bound to this
    envelope; ``timestamp_trust`` says whether its *time* is trusted. Without
    *tsa_anchors_pem* no TSA identity is checked, so a present, intact token is
    ``unanchored`` — a self-made TSA with any genTime reaches that state. With
    *tsa_anchors_pem* (operator TSA CA certificates, PEM) the token's TSA
    signature, EKU and chain are verified at its genTime (``anchored``, or
    ``invalid`` and the seal fails), and a missing token fails, as in ``nova
    verify --tsa-ca-bundle``: the token is outside the DSSE signature, so a
    deleted one could not otherwise be told apart from one never requested.
    """
    # Lexically absolute (``.`` and ``..`` folded, no link resolved) so the seal
    # directory has a name to be opened by, relative to its parent.
    seal_path = Path(os.path.abspath(seal_dir))
    errors: list[str] = []

    # Every ``.seal/`` file is read through ``_capsule_fs``: a symlink on its
    # path or a special file is a failure, never followed or waited on.
    envelope = _read_seal_member(
        seal_path, "manifest.dsse", max_bytes=_SEAL_ENVELOPE_MAX_BYTES
    )

    # --- Signature ---
    signature_ok = False
    pae_encoding: str | None = None
    signing_intent: SigningIntent | None = None
    signing_intent_status = "absent"
    dsse_bytes = b""
    if envelope.refused is not None:
        errors.append(f"manifest.dsse not read: {envelope.refused}")
    elif envelope.data is None:
        errors.append(f"Missing {envelope.path}")
    else:
        dsse_bytes = envelope.data
        try:
            pae_encoding = verify_envelope_encoding(dsse_bytes)
            signature_ok = True
            reading = read_intent(dsse_bytes)
            signing_intent, signing_intent_status = reading.intent, reading.status
            if reading.status == "mismatch":
                errors.append(
                    f"Signing intent label {reading.label!r} contradicts the signed intent "
                    "— the unsigned label was altered after signing (ADR-0309)"
                )
        except EnvelopeError as exc:
            errors.append(f"Signature verification failed: {exc}")

    # --- Timestamp ---
    ts = _check_seal_timestamp(
        _read_seal_member(seal_path, "manifest.dsse.tsr", max_bytes=_tsr_max_bytes()),
        dsse_bytes,
        tsa_anchors_pem,
    )
    errors.extend(ts.errors)

    # --- Signer identity (ADR-0301) — what the capsule alone can establish ---
    from novafabric.trust.novaseal.identity_trust import (  # noqa: PLC0415
        signer_certificate_info,
        unanchored_identity_trust,
    )

    signer = signer_certificate_info(dsse_bytes) if signature_ok else None

    # --- Merkle log inclusion ---
    payload_capsule_id = _payload_capsule_id(dsse_bytes) if signature_ok else None
    inclusion = _check_log_inclusion(
        _read_seal_member(seal_path, "log-entry.json", max_bytes=_SEAL_LOG_ENTRY_MAX_BYTES),
        payload_capsule_id,
        merkle_log,
        signer_keyid=signer.fingerprint_sha256 if signer else None,
        previous_logs=previous_logs,
    )
    log_notes = list(inclusion.notes)
    if inclusion.status == LOG_INCLUSION_FAILED:
        errors.extend(log_notes)
        log_notes = []
    log_integrity_ok = inclusion.status in (LOG_INCLUSION_LOCAL, LOG_INCLUSION_PROOF)

    # --- CA chain validation ---
    # ``_verify_ca_chain`` only *describes* the embedded certificate (self-signed, or
    # issuer named but not validated); no anchor is consulted here, so the chain is
    # never validated by this function. ADR-0301: report that as False — True printed
    # "ca_chain_ok=True" next to an unanchored, self-asserted identity.
    # ``nova verify --ca-bundle`` sets it when a real chain check passes.
    _described, ca_chain_errors = _verify_ca_chain(dsse_bytes)
    ca_chain_ok = False

    valid = (
        signature_ok
        and ts.ok is not False
        and not ts.anchor_failed
        and inclusion.status != LOG_INCLUSION_FAILED
        and signing_intent_status != "mismatch"
    )
    return VerificationResult(
        valid=valid,
        signature_ok=signature_ok,
        timestamp_ok=ts.ok,
        timestamp_present=ts.present,
        timestamp_strict=ts.strict,
        timestamp_trust=ts.trust,
        timestamp_gen_time=ts.gen_time,
        tsa_subject=ts.tsa_subject,
        log_integrity_ok=log_integrity_ok,
        errors=errors,
        signing_intent=signing_intent,
        signing_intent_status=signing_intent_status,
        ca_chain_ok=ca_chain_ok,
        ca_chain_errors=ca_chain_errors,
        pae_encoding=pae_encoding,
        log_inclusion=inclusion.status,
        log_notes=log_notes,
        sealer_log=inclusion.sealer_log,
        identity_trust=unanchored_identity_trust(signature_ok),
        signer_subject=signer.subject if signer else None,
        local_seal_identity=bool(signer and signer.local_seal_identity),
    )


# ---------------------------------------------------------------------------
# Timestamp trust
# ---------------------------------------------------------------------------


def timestamp_trust_for(timestamp_ok: bool | None, tsa_chain_ok: bool | None) -> str:
    """The ``TIMESTAMP_TRUST_*`` value for a token check and an optional anchored check.

    *timestamp_ok* is the anchor-free integrity result (None: no token);
    *tsa_chain_ok* is the result of the TSA trust-chain check against operator
    anchors, or None when no anchor was consulted. Only a passed anchored check
    makes the time trusted.
    """
    if timestamp_ok is None:
        return TIMESTAMP_TRUST_ABSENT
    if timestamp_ok is False or tsa_chain_ok is False:
        return TIMESTAMP_TRUST_INVALID
    if tsa_chain_ok is True:
        return TIMESTAMP_TRUST_ANCHORED
    return TIMESTAMP_TRUST_UNANCHORED


@dataclass(frozen=True)
class _TimestampOutcome:
    """What :func:`_check_seal_timestamp` established about ``manifest.dsse.tsr``."""

    ok: bool | None = None
    present: bool = False
    strict: bool = False
    trust: str = TIMESTAMP_TRUST_ABSENT
    gen_time: str | None = None
    tsa_subject: str | None = None
    anchor_failed: bool = False
    errors: tuple[str, ...] = ()


def _tsr_max_bytes() -> int:
    from novafabric.trust.novaseal.tsa_token import MAX_TOKEN_BYTES  # noqa: PLC0415

    return MAX_TOKEN_BYTES


def _check_seal_timestamp(
    tsr: _SealMember, dsse_bytes: bytes, tsa_anchors_pem: bytes | None
) -> _TimestampOutcome:
    """Check the seal's RFC 3161 token; anchor it when *tsa_anchors_pem* is given.

    ADR-0301: an absent or empty token is reported as absent (``ok`` None), not as
    a passed check, and does not invalidate the seal — timestamping is opt-in —
    unless TSA anchors were supplied (then the caller asked for a trusted time).
    """
    if tsr.refused is not None:
        # A token entry that is a link or a special file is not an absent token:
        # something was put where the token belongs, so the check fails.
        return _TimestampOutcome(
            ok=False,
            present=True,
            trust=TIMESTAMP_TRUST_INVALID,
            errors=(f"manifest.dsse.tsr not read: {tsr.refused}",),
        )
    tsr_bytes = tsr.data or b""
    if not tsr_bytes:
        if tsa_anchors_pem is None:
            return _TimestampOutcome()
        return _TimestampOutcome(
            anchor_failed=True,
            errors=(
                "no RFC 3161 token (manifest.dsse.tsr) although TSA trust anchors are "
                "configured; the token is not covered by the DSSE signature, so a missing "
                "one cannot be told apart from a deleted one",
            ),
        )
    if not dsse_bytes:
        return _TimestampOutcome(
            ok=False,
            present=True,
            trust=TIMESTAMP_TRUST_INVALID,
            errors=("Cannot verify TSR: DSSE envelope missing",),
        )
    check = check_timestamp(tsr_bytes, dsse_bytes)
    claimed = _TimestampOutcome(
        ok=check.ok,
        present=True,
        strict=check.strict,
        trust=timestamp_trust_for(check.ok, None),
        gen_time=check.gen_time.isoformat() if check.gen_time is not None else None,
        tsa_subject=check.tsa_subject,
        errors=() if check.ok else (f"TSR verification failed: {check.reason}",),
    )
    if not check.ok or tsa_anchors_pem is None:
        return claimed
    from novafabric.trust.novaseal.tsa_trust import (  # noqa: PLC0415
        verify_tsa_trust_chain,
    )

    anchored = verify_tsa_trust_chain(
        tsr_bytes, tsa_anchors_pem, None, expected_digest=hashlib.sha256(dsse_bytes).digest()
    )
    if anchored.valid:
        return replace(claimed, trust=TIMESTAMP_TRUST_ANCHORED)
    return replace(
        claimed,
        trust=TIMESTAMP_TRUST_INVALID,
        anchor_failed=True,
        errors=(f"TSA trust chain failed: {anchored.reason}",),
    )


def unanchored_claims(result: VerificationResult) -> list[str]:
    """What a passed *result* still rests on without an anchor the verifier trusts.

    ``nova verify --strict`` fails when this is non-empty. Each entry names one
    claim that verified only as self-consistent or self-asserted:

    * ``identity-self-asserted`` — no CA bundle validated the signer (ADR-0301);
    * ``timestamp-unanchored`` — a present token's TSA was not validated against
      a TSA trust anchor, so its genTime is not trusted time;
    * ``log-head-unanchored`` — inclusion rests on the carried proof, whose tree
      head is not signed or witnessed (ADR-0297 S2);
    * ``log-inclusion-not-checked`` — no proof and no log;
    * ``sealer-log-other-version`` — see ``SEALER_LOG_OTHER_VERSION``.

    An absent timestamp is not listed: it claims no time.
    """
    from novafabric.trust.novaseal.identity_trust import (  # noqa: PLC0415
        IDENTITY_SELF_ASSERTED,
    )

    claims: list[str] = []
    if result.identity_trust == IDENTITY_SELF_ASSERTED:
        claims.append("identity-self-asserted")
    if result.timestamp_trust == TIMESTAMP_TRUST_UNANCHORED:
        claims.append("timestamp-unanchored")
    if result.log_inclusion == LOG_INCLUSION_PROOF:
        claims.append("log-head-unanchored")
    elif result.log_inclusion == LOG_INCLUSION_NOT_CHECKED:
        claims.append("log-inclusion-not-checked")
    if result.sealer_log == SEALER_LOG_OTHER_VERSION:
        claims.append("sealer-log-other-version")
    return claims


def verify_with_profile(seal_dir: Path | str, profile: Any) -> VerificationResult:
    """Verify *seal_dir* with what a NovaSeal *profile* (``novaseal.yaml``) provides.

    Opens the configured ``merkle_db`` and every ``previous_merkle_dbs`` entry
    only if they exist (verification never creates a log) and anchors the
    timestamp with ``tsa_ca_certs`` when configured. *profile* may be None (no
    config on this machine): the capsule is then verified from its own files.
    Raises ``OSError`` when a configured TSA CA file cannot be read.
    """
    from novafabric.trust.novaseal.merkle import open_existing_merkle_log  # noqa: PLC0415

    anchors_pem: bytes | None = None
    tsa_ca_certs = list(getattr(profile, "tsa_ca_certs", None) or [])
    if tsa_ca_certs:
        anchors_pem = b"\n".join(Path(p).read_bytes() for p in tsa_ca_certs)
    current = open_existing_merkle_log(getattr(profile, "merkle_db", None))
    previous: list[MerkleLog | PostgresMerkleLog] = []
    try:
        for location in getattr(profile, "previous_merkle_dbs", None) or []:
            opened = open_existing_merkle_log(location)
            if opened is not None:
                previous.append(opened)
        return verify_seal_dir(
            seal_dir, merkle_log=current, previous_logs=previous, tsa_anchors_pem=anchors_pem
        )
    finally:
        for lg in (current, *previous):
            if lg is not None:
                lg.close()


# ---------------------------------------------------------------------------
# Merkle log inclusion
# ---------------------------------------------------------------------------


def _payload_capsule_id(dsse_bytes: bytes) -> str | None:
    """SHA-256 of the signed payload — the capsule id, derived, never read."""
    try:
        return hashlib.sha256(extract_payload(dsse_bytes)).hexdigest()
    except Exception:
        return None


def _is_int(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


@dataclass(frozen=True)
class _Inclusion:
    """``(log_inclusion, notes, sealer_log)`` — see :func:`verify_seal_dir`."""

    status: str
    notes: tuple[str, ...]
    sealer_log: str = SEALER_LOG_NOT_CONSULTED


def _failed(note: str, sealer_log: str = SEALER_LOG_NOT_CONSULTED) -> _Inclusion:
    return _Inclusion(LOG_INCLUSION_FAILED, (note,), sealer_log)


@dataclass(frozen=True)
class _LogRecord:
    """The fields of ``log-entry.json`` the log checks use, already validated."""

    leaf_index: int
    leaf_hash: str
    root_hash: str | None
    tree_size: int | None
    root_version: int
    keyids: tuple[object, ...]


def _check_log_inclusion(
    log_file: _SealMember,
    payload_capsule_id: str | None,
    merkle_log: "MerkleLog | PostgresMerkleLog | None",
    *,
    signer_keyid: str | None = None,
    previous_logs: "Sequence[MerkleLog | PostgresMerkleLog]" = (),
) -> _Inclusion:
    """Check ``log-entry.json`` and, when given, the supplied logs — see :func:`verify_seal_dir`.

    *signer_keyid* is the SHA-256 fingerprint of the certificate whose signature
    verified; with the entry's own ``keyid`` it decides which supplied logs are
    this signer's (:func:`_sealer_log_keyid`).
    """
    parsed = _parse_log_record(log_file, payload_capsule_id, signer_keyid)
    if isinstance(parsed, _Inclusion):
        return parsed
    record, proof = parsed

    status = LOG_INCLUSION_NOT_CHECKED
    notes: list[str] = []
    if proof is not None:
        carried = _check_carried_proof(record, proof)
        if carried is not None:
            return carried
        status = LOG_INCLUSION_PROOF
        notes.append(
            "inclusion proof carried in the capsule verifies against the tree head "
            f"recorded at seal time (size {record.tree_size}, root v{record.root_version}); "
            "that head is not independently anchored"
        )
        if record.root_version == ROOT_VERSION_V1:
            notes.append(
                "root v1 (sealed before ADR-0313) does not commit to the tree size; the "
                "size is taken from log-entry.json and only the sealer's log can confirm it"
            )

    logs = [lg for lg in (merkle_log, *previous_logs) if lg is not None]
    sealer_log = SEALER_LOG_NO_LOG
    if logs:
        try:
            for lg in logs:
                held = _check_entry_in_log(lg, record)
                if held is not None:
                    return held
            verdict = _sealer_log_verdict(logs, record)
        except MerkleError as exc:
            return _failed(f"Merkle verification error: {exc}")
        if verdict.status == LOG_INCLUSION_FAILED:
            return verdict
        notes.extend(verdict.notes)
        sealer_log = verdict.sealer_log

    if status == LOG_INCLUSION_NOT_CHECKED:
        notes.append(
            "log not available — inclusion not checked (this capsule carries no inclusion "
            "proof; verify against the sealer's log for that check)"
        )
    return _Inclusion(status, tuple(notes), sealer_log)


def _parse_log_record(
    log_file: _SealMember, payload_capsule_id: str | None, signer_keyid: str | None
) -> "tuple[_LogRecord, object] | _Inclusion":
    """Validate ``log-entry.json``; return the record and its raw proof, or a failure."""
    if log_file.refused is not None:
        return _failed(f"log-entry.json not read: {log_file.refused}")
    if log_file.data is None:
        return _failed(f"Missing {log_file.path}")
    try:
        record = json.loads(log_file.data)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return _failed(f"Merkle verification error: {exc}")
    if not isinstance(record, dict):
        return _failed("log-entry.json is not a JSON object")

    leaf_index = record.get("leaf_index")
    if leaf_index is None:
        return _failed("log-entry.json missing leaf_index")
    entry = record.get("entry")
    if not _is_int(leaf_index) or leaf_index < 0 or not isinstance(entry, dict):
        return _failed("log-entry.json has a malformed leaf_index or entry")

    # The leaf must be *this* entry, and the entry must name *this* capsule.
    leaf_hash = _leaf_hash(json.dumps(entry, sort_keys=True, separators=(",", ":")).encode())
    recorded_leaf = record.get("leaf_hash")
    if recorded_leaf is not None and recorded_leaf != leaf_hash:
        return _failed("log-entry.json entry does not hash to its recorded leaf_hash")
    if payload_capsule_id is not None and entry.get("capsule_id") != payload_capsule_id:
        return _failed(
            "log-entry.json names a different capsule than the signed payload "
            f"({entry.get('capsule_id')!r} != {payload_capsule_id!r})"
        )

    # ADR-0313: the root version selects the tree construction; absent means v1.
    try:
        root_version = root_version_of(record)
    except UnsupportedRootVersionError as exc:
        return _failed(f"log-entry.json: {exc}")
    root_hash = record.get("root_hash")
    tree_size = record.get("tree_size")
    if root_hash is not None and tree_size is None:
        return _failed(
            "log-entry.json records a root_hash without its tree_size; a Merkle root "
            "is verified only together with its tree size (ADR-0313)"
        )
    if tree_size is not None and (not _is_int(tree_size) or tree_size <= leaf_index):
        return _failed("log-entry.json has a malformed tree_size")
    parsed = _LogRecord(
        leaf_index=leaf_index,
        leaf_hash=leaf_hash,
        root_hash=root_hash if isinstance(root_hash, str) else None,
        tree_size=tree_size if _is_int(tree_size) else None,
        root_version=root_version,
        keyids=(entry.get("keyid"), signer_keyid),
    )
    if root_hash is not None and parsed.root_hash is None:
        return _failed("log-entry.json has a malformed root_hash")
    return parsed, record.get("inclusion_proof")


def _check_carried_proof(record: _LogRecord, proof: object) -> _Inclusion | None:
    """Verify the carried inclusion proof; a failure, or None when it verifies."""
    if (
        not isinstance(proof, list)
        or not all(isinstance(h, str) for h in proof)
        or record.root_hash is None
        or record.tree_size is None
        or (
            record.root_version == ROOT_VERSION_V1
            and len(proof) != inclusion_proof_length(record.tree_size)
        )
    ):
        return _failed("log-entry.json carries a malformed inclusion_proof")
    try:
        proof_ok = verify_inclusion_proof(
            record.leaf_hash,
            record.leaf_index,
            proof,
            record.root_hash,
            record.tree_size,
            root_version=record.root_version,
        )
    except ValueError:  # a sibling that is not hex
        proof_ok = False
    if not proof_ok:
        return _failed(
            f"Merkle inclusion proof failed for leaf_index={record.leaf_index} "
            f"(the carried proof does not recompute root {record.root_hash})"
        )
    return None


def _check_entry_in_log(
    merkle_log: "MerkleLog | PostgresMerkleLog", record: _LogRecord
) -> _Inclusion | None:
    """Check the entry against one log: ``local-log`` or ``failed`` if it holds it, else None."""
    found = SEALER_LOG_ENTRY_FOUND
    # Ask about the recorded position, not about the first match: sealing an
    # identical manifest twice appends a byte-identical leaf, and the lowest
    # index holding it is the *earlier* seal's.
    if _leaf_hash_at(merkle_log, record.leaf_index) != record.leaf_hash:
        elsewhere = merkle_log.find_leaf_index(record.leaf_hash)
        if elsewhere is None:
            return None
        return _failed(
            f"log-entry.json says leaf_index={record.leaf_index} but the local log "
            f"holds this entry at leaf_index={elsewhere}, not at {record.leaf_index}",
            found,
        )
    # The record's root version is not compared with the log's here: the entry is
    # *in* this log, and the recorded head is recomputed below from the log's own
    # leaves under the record's version, so a wrong label cannot make it pass.
    if not merkle_log.verify_entry(record.leaf_index):
        return _failed(f"Merkle inclusion proof failed for leaf_index={record.leaf_index}", found)
    # ADR-0313: the recorded tree head (size AND root) must be one the log's own
    # leaves reproduce — recomputed, never read from a stored head.
    if record.root_hash is not None and record.tree_size is not None:
        reproduced = merkle_log.compute_root_at_size(
            record.tree_size, root_version=record.root_version
        )
        if reproduced != record.root_hash:
            return _failed(
                f"log-entry.json records a tree head (size {record.tree_size}, root "
                f"{record.root_hash[:16]}…, v{record.root_version}) that the local log's "
                "leaves do not reproduce",
                found,
            )
    return _Inclusion(
        LOG_INCLUSION_LOCAL,
        ("entry found in the local Merkle log and included under its root",),
        found,
    )


def _sealer_log_verdict(
    logs: "Sequence[MerkleLog | PostgresMerkleLog]", record: _LogRecord
) -> _Inclusion:
    """Decide what absence from every supplied log means — see :func:`verify_seal_dir`.

    The returned status is ``failed`` (absent from the sealer's log) or
    ``not-checked`` (absence decides nothing; the caller keeps its own status and
    takes the notes and ``sealer_log``).
    """
    signer_logs: list[tuple[str, int]] = []
    for lg in logs:
        keyid = _sealer_log_keyid(lg, record.keyids)
        if keyid is not None:
            signer_logs.append((keyid, lg.root_version))
    rv = record.root_version
    if not signer_logs:
        return _Inclusion(
            LOG_INCLUSION_NOT_CHECKED,
            (
                "entry is not in the local Merkle log, which holds no seals by this "
                "signer (a different or fresh log)",
            ),
            SEALER_LOG_NOT_SEALERS,
        )
    same = [k for k, v in signer_logs if v == rv]
    if same:
        return _failed(
            "this capsule's entry is not in the local Merkle log, yet that log "
            f"holds other seals by the same signer (keyid {same[0][:16]}…, "
            f"root v{rv}): it is the sealer's log, and a carried proof "
            "cannot stand in for it. The log was rolled back or replaced, or the "
            "capsule was signed outside the sealing path. (If another host sealed "
            "it into its own log with a shared key, verify with that log, or with "
            "a novaseal.yaml whose merkle_db does not exist.)",
            SEALER_LOG_MISSING,
        )
    newest_keyid, newest = max(signer_logs, key=lambda kv: kv[1])
    if newest < rv:
        return _failed(
            f"log-entry.json claims root v{rv}, but the sealer's local Merkle log "
            f"(keyid {newest_keyid[:16]}…) is a v{newest} log and does not hold this "
            f"entry. v{rv} logs came after v{newest} logs (ADR-0313), so this host's "
            f"v{newest} log did not seal it: the record's root version was rewritten, "
            "the log was replaced, or the capsule was signed outside the sealing path.",
            SEALER_LOG_MISSING,
        )
    versions = ", ".join(f"v{v}" for v in sorted({v for _k, v in signer_logs}))
    return _Inclusion(
        LOG_INCLUSION_NOT_CHECKED,
        (
            f"UNVERIFIED: the sealer's local Merkle log ({versions}) is newer than this "
            f"capsule's root v{rv} and does not hold it, and no v{rv} log of this host "
            "was supplied. A capsule sealed before the ADR-0313 migration looks exactly "
            f"like a key holder's forgery that claims v{rv} — root_version is written "
            "by whoever wrote log-entry.json. Add the host's earlier log to "
            "previous_merkle_dbs in novaseal.yaml to decide it.",
        ),
        SEALER_LOG_OTHER_VERSION,
    )


def _leaf_hash_at(
    merkle_log: "MerkleLog | PostgresMerkleLog", leaf_index: int
) -> str | None:
    """The leaf hash the log stores at *leaf_index*, or None when it holds none."""
    try:
        return merkle_log.get_leaf_hash(leaf_index)
    except MerkleError:
        return None


def _sealer_log_keyid(
    merkle_log: "MerkleLog | PostgresMerkleLog",
    keyids: tuple[object, ...],
) -> str | None:
    """Return the keyid that makes *merkle_log* one of this signer's logs, else None.

    A log is the signer's when it holds seals by the same signer — the entry's
    ``keyid`` or the fingerprint of the certificate whose signature verified.
    The root version is deliberately *not* part of this test: it is read from
    ``log-entry.json``, which the capsule's writer controls, so letting it decide
    whether a log counts handed a forger the bypass (claim the other version).
    :func:`_sealer_log_verdict` compares the versions instead.
    """
    for keyid in dict.fromkeys(k for k in keyids if isinstance(k, str)):
        if merkle_log.has_entries_for_keyid(keyid):
            return keyid
    return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _keyid_from_cert(cert_path: Path) -> str:
    from cryptography.hazmat.primitives import serialization
    from cryptography.x509 import load_pem_x509_certificate

    pem = cert_path.read_bytes()
    cert = load_pem_x509_certificate(pem)
    der = cert.public_bytes(serialization.Encoding.DER)
    return hashlib.sha256(der).hexdigest()


def _verify_ca_chain(dsse_bytes: bytes) -> tuple[bool, list[str]]:
    """Verify the CA chain for the signing cert embedded in a DSSE envelope.

    Extracts the ``cert`` field (base64-encoded DER) from the first signature
    in the DSSE envelope and checks whether the issuer differs from the subject
    (i.e. not self-signed).  If not self-signed, attempts to build the issuer
    chain using ``cryptography.x509``.

    Returns:
        ``(ca_chain_ok, ca_chain_errors)``

    Degrades safely:
        - If ``cryptography`` is not importable → returns ``(False, [note])``
        - If the DSSE envelope is empty or unparseable → returns ``(False, [note])``
        - If self-signed → returns ``(True, [note])``
    """
    if not dsse_bytes:
        return False, ["DSSE envelope is empty — cannot verify CA chain"]

    try:
        import base64
        import json

        from cryptography.x509 import load_der_x509_certificate

        envelope = json.loads(dsse_bytes)
        sigs = envelope.get("signatures", [])
        if not sigs:
            return False, ["No signatures in DSSE envelope"]

        cert_b64 = sigs[0].get("cert")
        if not cert_b64:
            return False, ["No cert field in DSSE signature entry"]

        # Support both standard base64 and base64url
        cert_der = base64.urlsafe_b64decode(cert_b64 + "==")
        cert = load_der_x509_certificate(cert_der)

        # Self-signed check
        if cert.issuer == cert.subject:
            return True, ["Signing cert is self-signed (no issuer chain to validate)"]

        # CA-signed: the issuer chain cannot be fully validated without access to
        # the intermediate/root CA certs.  We record the issuer DN and degrade
        # gracefully with a note — full chain building requires an explicit
        # trusted CA bundle (pass ``trusted_ca_pem`` to ``verify_tsa_chain``).
        issuer_dn = cert.issuer.rfc4514_string()
        return True, [
            f"CA-signed cert (issuer: {issuer_dn}); "
            "full chain not validated (no CA bundle supplied to NovaSeal.verify)"
        ]

    except ImportError:
        return False, ["cryptography package not importable — CA chain not verified"]
    except Exception as exc:
        return False, [f"CA chain verification error: {exc}"]
