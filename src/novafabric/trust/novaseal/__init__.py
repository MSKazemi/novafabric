"""NovaSeal v0.1 — Cryptographic signing core for NovaFabric capsules.

Implements ADR-0041 (local-key mode only in v0.1):
- DSSE envelope with ECDSA P-256 / SHA-256 (ADR-001)
- RFC 3161 trusted timestamps via FreeTSA (ADR-007)
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
        tsa_url="https://freetsa.org/tsr",
        db_path="/path/to/merkle.db",
    )
    bundle = seal.seal(capsule_manifest)
    result = seal.verify(capsule_id, seal_dir)
    assert result.signature_ok and result.timestamp_ok and result.log_integrity_ok
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from novafabric.trust.novaseal.envelope import (
    EnvelopeError,
    SigningIntent,
    create_envelope,
    extract_intent,
    extract_payload,
    verify_envelope_encoding,
)
from novafabric.trust.novaseal.merkle import (  # noqa: F401
    MerkleError,
    MerkleLog,
    PostgresMerkleLog,
    _leaf_hash,
    inclusion_proof_length,
    open_merkle_log,
    verify_inclusion_proof,
)
from novafabric.trust.novaseal.nonce_store import NonceStore
from novafabric.trust.novaseal.timestamp import (
    TSAUnavailableError,
    request_timestamp,
    verify_timestamp,
)
from novafabric.trust.novaseal.trust_chain import CertChainResult, verify_tsa_cert_chain

if TYPE_CHECKING:  # pragma: no cover — typing only
    from novafabric.trust.novaseal.signing_backend import SigningBackend

log = logging.getLogger(__name__)

#: Merkle inclusion established against the verifier's local (sealer's) log.
LOG_INCLUSION_LOCAL = "local-log"
#: Merkle inclusion established from the proof carried in ``log-entry.json``.
LOG_INCLUSION_PROOF = "carried-proof"
#: No proof carried and the entry is not in any available log — not checked.
LOG_INCLUSION_NOT_CHECKED = "not-checked"
#: The log metadata is inconsistent with the capsule or with the log.
LOG_INCLUSION_FAILED = "failed"

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
    timestamp_ok: bool
    log_integrity_ok: bool
    errors: list[str] = field(default_factory=list)  # noqa: RUF009
    signing_intent: SigningIntent | None = None
    ca_chain_ok: bool = False
    ca_chain_errors: list[str] = field(default_factory=list)  # noqa: RUF009
    # Whether an RFC 3161 token was actually present and verified. ``timestamp_ok``
    # is deliberately True when timestamping was skipped (the TSA is best-effort),
    # so it alone cannot tell "timestamped" from "never timestamped" — and
    # reporting a bare "Timestamp: OK" for a capsule that carries no token
    # overstates the evidence.
    timestamp_present: bool = False
    # Which DSSE PAE the signature verified over: ``"dsse-v1"`` (spec; verifiable
    # with stock DSSE tooling) or ``"legacy-le64"`` (sealed through v0.102.x;
    # verifiable with NovaFabric only). None when the signature did not verify.
    pae_encoding: str | None = None
    # How Merkle inclusion was established — see ``verify_seal_dir``. One of
    # LOG_INCLUSION_LOCAL / _PROOF / _NOT_CHECKED / _FAILED. ``log_integrity_ok``
    # is True only for the first two; ``not-checked`` does not invalidate.
    log_inclusion: str = "failed"
    log_notes: list[str] = field(default_factory=list)  # noqa: RUF009

    def __str__(self) -> str:
        parts = [
            f"signature_ok={self.signature_ok}",
            f"timestamp_ok={self.timestamp_ok}",
            f"log_integrity_ok={self.log_integrity_ok}",
            f"log_inclusion={self.log_inclusion}",
            f"ca_chain_ok={self.ca_chain_ok}",
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
        tsa_url:   RFC 3161 TSA URL (use "" to disable timestamping).
        db_path:   Path to the SQLite Merkle log database.
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
        self._key_path = (
            Path(config.key_path)
            if config.key_path and config.key_path != "None"
            else Path()
        )
        self._cert_path = Path(config.cert_path)
        self._tsa_url = tsa_url
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

        # 2. RFC 3161 timestamp (best-effort; warns but does not fail if TSA is down)
        tsr_bytes = b""
        if self._tsa_url:
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

    def verify(self, capsule_id: str, seal_dir: str) -> VerificationResult:
        """Verify signature, TSR, and Merkle inclusion for a capsule.

        Delegates to :func:`verify_seal_dir` with this instance's Merkle log, so
        the sealer's own log is consulted when it holds the entry.

        Args:
            capsule_id: Unused; kept for API compatibility. The capsule id is
                        re-derived from the signed payload (ADR-0251 §2).
            seal_dir:   Path to the .seal/ directory inside the capsule dir.
        """
        del capsule_id
        return verify_seal_dir(Path(seal_dir), merkle_log=self._merkle)

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


def verify_seal_dir(
    seal_dir: Path | str,
    merkle_log: "MerkleLog | PostgresMerkleLog | None" = None,
) -> VerificationResult:
    """Verify a capsule's ``.seal/`` directory from the capsule alone.

    Signature and timestamp need nothing but the files. Merkle inclusion is
    checked by every means available and reported in ``log_inclusion``:

    * ``carried-proof`` — ``log-entry.json`` carries ``inclusion_proof`` (sealed
      from v0.103 on) and it recomputes the recorded ``root_hash``;
    * ``local-log`` — *merkle_log* (the sealer's log) holds this exact leaf at the
      recorded index and it is included under the log's current root;
    * ``not-checked`` — neither is available (a v0.102.x seal verified away from
      the sealer's log). The capsule still verifies; the caller must say so;
    * ``failed`` — anything inconsistent: the entry does not hash to its
      ``leaf_hash``, names another capsule, carries a proof that does not verify,
      or sits at a different index in the supplied log.

    A carried proof binds the entry to the tree head recorded at seal time. That
    head is *not* independently anchored here (it is not signed or witnessed), so
    ``carried-proof`` proves the capsule's log metadata is self-consistent, not
    that a third party saw the log; checking a published or witnessed root is the
    job of ``nova seal log verify`` / ADR-0097.
    """
    seal_path = Path(seal_dir)
    errors: list[str] = []

    # --- Signature ---
    signature_ok = False
    pae_encoding: str | None = None
    signing_intent: SigningIntent | None = None
    dsse_bytes = b""
    dsse_file = seal_path / "manifest.dsse"
    if not dsse_file.exists():
        errors.append(f"Missing {dsse_file}")
    else:
        dsse_bytes = dsse_file.read_bytes()
        try:
            pae_encoding = verify_envelope_encoding(dsse_bytes)
            signature_ok = True
            signing_intent = extract_intent(dsse_bytes)
        except EnvelopeError as exc:
            errors.append(f"Signature verification failed: {exc}")

    # --- Timestamp ---
    timestamp_ok = False
    timestamp_present = False
    tsr_file = seal_path / "manifest.dsse.tsr"
    if not tsr_file.exists():
        # TSA may have been skipped — treat as ok if TSR file absent
        timestamp_ok = True
    else:
        tsr_bytes = tsr_file.read_bytes()
        if not tsr_bytes:
            # Empty TSR = TSA was explicitly skipped
            timestamp_ok = True
        elif dsse_bytes:
            timestamp_present = True
            timestamp_ok = verify_timestamp(tsr_bytes, dsse_bytes)
            if not timestamp_ok:
                errors.append("TSR verification failed: hash mismatch or invalid DER")
        else:
            errors.append("Cannot verify TSR: DSSE envelope missing")

    # --- Merkle log inclusion ---
    payload_capsule_id = _payload_capsule_id(dsse_bytes) if signature_ok else None
    log_inclusion, log_notes = _check_log_inclusion(
        seal_path / "log-entry.json", payload_capsule_id, merkle_log
    )
    if log_inclusion == LOG_INCLUSION_FAILED:
        errors.extend(log_notes)
        log_notes = []
    log_integrity_ok = log_inclusion in (LOG_INCLUSION_LOCAL, LOG_INCLUSION_PROOF)

    # --- CA chain validation ---
    ca_chain_ok, ca_chain_errors = _verify_ca_chain(dsse_bytes)

    valid = signature_ok and timestamp_ok and log_inclusion != LOG_INCLUSION_FAILED
    return VerificationResult(
        valid=valid,
        signature_ok=signature_ok,
        timestamp_ok=timestamp_ok,
        timestamp_present=timestamp_present,
        log_integrity_ok=log_integrity_ok,
        errors=errors,
        signing_intent=signing_intent,
        ca_chain_ok=ca_chain_ok,
        ca_chain_errors=ca_chain_errors,
        pae_encoding=pae_encoding,
        log_inclusion=log_inclusion,
        log_notes=log_notes,
    )


def _payload_capsule_id(dsse_bytes: bytes) -> str | None:
    """SHA-256 of the signed payload — the capsule id, derived, never read."""
    try:
        return hashlib.sha256(extract_payload(dsse_bytes)).hexdigest()
    except Exception:
        return None


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _check_log_inclusion(
    log_file: Path,
    payload_capsule_id: str | None,
    merkle_log: "MerkleLog | PostgresMerkleLog | None",
) -> tuple[str, list[str]]:
    """Return ``(log_inclusion status, notes)`` — see :func:`verify_seal_dir`."""
    if not log_file.exists():
        return LOG_INCLUSION_FAILED, [f"Missing {log_file}"]
    try:
        record = json.loads(log_file.read_bytes())
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return LOG_INCLUSION_FAILED, [f"Merkle verification error: {exc}"]
    if not isinstance(record, dict):
        return LOG_INCLUSION_FAILED, ["log-entry.json is not a JSON object"]

    leaf_index = record.get("leaf_index")
    if leaf_index is None:
        return LOG_INCLUSION_FAILED, ["log-entry.json missing leaf_index"]
    entry = record.get("entry")
    if not _is_int(leaf_index) or leaf_index < 0 or not isinstance(entry, dict):
        return LOG_INCLUSION_FAILED, ["log-entry.json has a malformed leaf_index or entry"]

    # The leaf must be *this* entry, and the entry must name *this* capsule.
    leaf_hash = _leaf_hash(json.dumps(entry, sort_keys=True, separators=(",", ":")).encode())
    recorded_leaf = record.get("leaf_hash")
    if recorded_leaf is not None and recorded_leaf != leaf_hash:
        return LOG_INCLUSION_FAILED, [
            "log-entry.json entry does not hash to its recorded leaf_hash"
        ]
    if payload_capsule_id is not None and entry.get("capsule_id") != payload_capsule_id:
        return LOG_INCLUSION_FAILED, [
            "log-entry.json names a different capsule than the signed payload "
            f"({entry.get('capsule_id')!r} != {payload_capsule_id!r})"
        ]

    status = LOG_INCLUSION_NOT_CHECKED
    notes: list[str] = []

    proof = record.get("inclusion_proof")
    if proof is not None:
        root_hash = record.get("root_hash")
        tree_size = record.get("tree_size")
        if (
            not isinstance(proof, list)
            or not all(isinstance(h, str) for h in proof)
            or not isinstance(root_hash, str)
            or not isinstance(tree_size, int)
            or isinstance(tree_size, bool)
            or tree_size <= leaf_index
            or len(proof) != inclusion_proof_length(tree_size)
        ):
            return LOG_INCLUSION_FAILED, ["log-entry.json carries a malformed inclusion_proof"]
        try:
            proof_ok = verify_inclusion_proof(leaf_hash, leaf_index, proof, root_hash, tree_size)
        except ValueError:  # a sibling that is not hex
            proof_ok = False
        if not proof_ok:
            return LOG_INCLUSION_FAILED, [
                f"Merkle inclusion proof failed for leaf_index={leaf_index} "
                f"(the carried proof does not recompute root {root_hash})"
            ]
        status = LOG_INCLUSION_PROOF
        notes.append(
            "inclusion proof carried in the capsule verifies against the tree head "
            f"recorded at seal time (size {tree_size}); that head is not independently "
            "anchored"
        )

    if merkle_log is not None:
        try:
            found = merkle_log.find_leaf_index(leaf_hash)
            if found is not None:
                if found != leaf_index:
                    return LOG_INCLUSION_FAILED, [
                        f"log-entry.json says leaf_index={leaf_index} but the local log "
                        f"holds this entry at leaf_index={found}"
                    ]
                if not merkle_log.verify_entry(leaf_index):
                    return LOG_INCLUSION_FAILED, [
                        f"Merkle inclusion proof failed for leaf_index={leaf_index}"
                    ]
                status = LOG_INCLUSION_LOCAL
                notes.append("entry found in the local Merkle log and included under its root")
            elif status == LOG_INCLUSION_NOT_CHECKED:
                notes.append("entry is not in the local Merkle log (a different or fresh log)")
        except MerkleError as exc:
            return LOG_INCLUSION_FAILED, [f"Merkle verification error: {exc}"]

    if status == LOG_INCLUSION_NOT_CHECKED:
        notes.append(
            "log not available — inclusion not checked (this capsule carries no inclusion "
            "proof; verify against the sealer's log for that check)"
        )
    return status, notes


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
