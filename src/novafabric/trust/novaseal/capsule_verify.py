"""Verify a sealed capsule *directory* — the one path every surface uses.

``verify_seal_dir`` verifies ``.seal/`` on its own: the signature, the RFC 3161
token and the Merkle log entry. It never opens the capsule around it, so it
cannot tell whether the signed manifest describes the directory it sits in.
ADR-0251 adds that binding (``capsule.yaml`` against the signed payload,
``evidence_digests`` against the files, the log entry's ``capsule_id`` against
the payload hash), and ADR-0055/ADR-0070 add the optional signer and TSA chain
checks against operator anchors.

Until audit H1 (2026-10-22) those extra checks lived in ``cli/verify.py`` only.
The trust radar, the dashboard Seal tab and ``POST /api/runs/{id}/verify``
called the NovaSeal checks alone, so a capsule edited after sealing — or one
carrying another capsule's genuine ``.seal/`` — read as ``attested`` there while
``nova verify`` exited 1. :func:`verify_capsule` is now the single
implementation; the CLI only prints what it returns.

Offline and read-only: no network, no log is created (ADR-0297 S2).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from novafabric._capsule_fs import (
    CapsuleEntryError,
    open_capsule_file,
    read_capsule_file,
)
from novafabric.trust.novaseal import (
    VerificationResult,
    timestamp_trust_for,
    verify_seal_dir,
)
from novafabric.trust.novaseal.envelope import SEAL_METADATA_KEY, extract_payload

__all__ = [
    "AnchorCheck",
    "CapsuleBinding",
    "CapsuleVerification",
    "capsule_binding",
    "derive_capsule_id",
    "read_capsule_tsr",
    "signer_chain_check",
    "tsa_chain_check",
    "verify_capsule",
]

_OPERATION = "nova verify"
# Bounds for the small structured files read whole (R6: no unbounded read of a
# capsule file). Evidence files are hashed in chunks, never read whole.
_MAX_ENVELOPE_BYTES = 16 * 1024 * 1024
_MAX_LOG_ENTRY_BYTES = 1024 * 1024
_MAX_MANIFEST_BYTES = 16 * 1024 * 1024
_HASH_CHUNK = 1 << 20

_NO_TOKEN_REASON = (
    "no RFC 3161 token (manifest.dsse.tsr) although TSA trust anchors are "
    "configured; the token is not covered by the DSSE signature, so a missing "
    "one cannot be told apart from a deleted one"
)


@dataclass(frozen=True)
class CapsuleBinding:
    """ADR-0251: is the seal bound to *this* directory?

    ``manifest_present`` / ``digests_present`` False means the check could not
    run (no ``capsule.yaml``; sealed before ``evidence_digests``) — reported as
    NOT PRESENT, never as passed and never as failed (ADR-0251 §4).
    """

    manifest_present: bool = False
    manifest_ok: bool = False
    differing_keys: tuple[str, ...] = ()
    digests_present: bool = False
    digests_ok: bool = False
    mismatched: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    unlisted: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        """False when any check that ran failed; absent checks do not fail."""
        manifest = self.manifest_ok or not self.manifest_present
        digests = self.digests_ok or not self.digests_present
        return manifest and digests and not self.errors

    def failure_reasons(self) -> list[str]:
        """One human-readable line per failed binding check."""
        reasons = list(self.errors)
        if self.manifest_present and not self.manifest_ok:
            if self.differing_keys:
                reasons.append(
                    "capsule.yaml disagrees with the signed payload: "
                    + ", ".join(self.differing_keys)
                )
            elif not self.errors:
                reasons.append("capsule.yaml does not match the signed payload")
        reasons.extend(f"evidence file modified after sealing: {p}" for p in self.mismatched)
        reasons.extend(f"evidence file missing: {p}" for p in self.missing)
        return reasons


@dataclass(frozen=True)
class AnchorCheck:
    """Outcome of a chain check against verifier-supplied anchors (signer or TSA).

    ``revocation`` is the raw ``RevocationCheckResult`` (or None); ``anchor`` is
    the bundle certificate a signer chain ended at, which decides
    ``local-ca-pinned`` vs ``ca-anchored`` (ADR-0301).
    """

    ok: bool
    reason: str
    revocation: Any = None
    anchor: Any = None
    gen_time: str | None = None
    signer_subject: str | None = None
    chain_subjects: tuple[str, ...] = ()


@dataclass
class CapsuleVerification:
    """Everything ``nova verify <capsule-dir>`` establishes about one capsule."""

    capsule_dir: Path
    result: VerificationResult
    binding: CapsuleBinding
    log_entry_capsule_id: str = ""
    derived_capsule_id: str = ""
    chain_check: AnchorCheck | None = None
    tsa_check: AnchorCheck | None = None
    # Capsule files that could not be read safely (a linked or special
    # ``.seal/manifest.dsse``): each one fails the verification.
    errors: list[str] = field(default_factory=list)

    @property
    def capsule_id_ok(self) -> bool:
        """The log entry's ``capsule_id`` equals the hash of the signed payload."""
        return not (
            self.derived_capsule_id
            and self.log_entry_capsule_id
            and self.derived_capsule_id != self.log_entry_capsule_id
        )

    @property
    def chain_ok(self) -> bool:
        return self.chain_check is None or self.chain_check.ok

    @property
    def tsa_ok(self) -> bool:
        return self.tsa_check is None or self.tsa_check.ok

    @property
    def seal_binds_capsule(self) -> bool:
        """The signature verifies, is over *this* directory, and its signer passed
        every chain check that was asked for. The radar's Signature axis."""
        return (
            self.result.signature_ok
            and self.binding.ok
            and self.capsule_id_ok
            and self.chain_ok
            and self.result.signing_intent_status != "mismatch"
            and not self.errors
        )

    @property
    def valid(self) -> bool:
        """The overall verdict ``nova verify`` exits on (0 when True, 1 when False)."""
        return (
            self.result.valid
            and self.binding.ok
            and self.capsule_id_ok
            and self.chain_ok
            and self.tsa_ok
            and not self.errors
        )

    def all_errors(self) -> list[str]:
        """Every failure reason, NovaSeal's first, then binding, id and chains."""
        errors = list(self.result.errors)
        errors.extend(self.binding.failure_reasons())
        if not self.capsule_id_ok:
            errors.append(
                f"log-entry.json capsule_id {self.log_entry_capsule_id} does not match "
                f"the signed payload ({self.derived_capsule_id})"
            )
        if self.chain_check is not None and not self.chain_check.ok:
            errors.append(f"signer certificate chain: {self.chain_check.reason}")
        if self.tsa_check is not None and not self.tsa_check.ok:
            errors.append(f"TSA certificate chain: {self.tsa_check.reason}")
        errors.extend(self.errors)
        return errors


def derive_capsule_id(dsse_bytes: bytes) -> str:
    """SHA-256 of the DSSE payload — the capsule_id, recomputed rather than read.

    ADR-0251 §2: an identifier read from ``log-entry.json``, a file inside the
    capsule an attacker can rewrite, and never checked is a suggestion.
    """
    if not dsse_bytes:
        return ""
    try:
        return hashlib.sha256(extract_payload(dsse_bytes)).hexdigest()
    except Exception:  # noqa: BLE001 — an undecodable envelope has no derivable id
        return ""


def _log_entry_capsule_id(capsule_dir: Path) -> str:
    try:
        entry = json.loads(
            read_capsule_file(
                capsule_dir,
                ".seal/log-entry.json",
                operation=_OPERATION,
                max_bytes=_MAX_LOG_ENTRY_BYTES,
            )
        )
        value = entry.get("entry", {}).get("capsule_id", "")
    except (OSError, ValueError, AttributeError, CapsuleEntryError):
        return ""
    return value if isinstance(value, str) else ""


def _signed_manifest(dsse_bytes: bytes) -> tuple[dict[str, Any] | None, str | None]:
    try:
        signed = json.loads(extract_payload(dsse_bytes))
    except Exception as exc:  # noqa: BLE001 — reported, never raised
        return None, f"Cannot decode signed payload: {exc}"
    if not isinstance(signed, dict):
        return None, "Signed payload is not a manifest object"
    return signed, None


def _manifest_binding(
    capsule_dir: Path, signed: dict[str, Any]
) -> tuple[bool, bool, tuple[str, ...], tuple[str, ...]]:
    """``(present, ok, differing_keys, errors)`` for ``capsule.yaml`` (ADR-0251 §2)."""
    manifest_file = capsule_dir / "capsule.yaml"
    if not manifest_file.exists():
        # Not every sealed directory is a run capsule — the object capsule store
        # seals a single-field manifest with no capsule.yaml. Absent, not failed.
        return False, False, (), ()
    import yaml  # noqa: PLC0415

    try:
        # No link is followed: a linked capsule.yaml is reported, never compared
        # with the target's contents (R6).
        on_disk = yaml.safe_load(
            read_capsule_file(
                capsule_dir, "capsule.yaml", operation=_OPERATION, max_bytes=_MAX_MANIFEST_BYTES
            )
        )
    except (OSError, ValueError, yaml.YAMLError, CapsuleEntryError) as exc:
        return True, False, (), (f"Cannot read capsule.yaml: {exc}",)
    if not isinstance(on_disk, dict):
        if on_disk is None:
            return True, False, (), ()
        return True, False, (), ("capsule.yaml did not parse as a mapping",)
    # ``_novaseal`` is seal metadata bound into the signed payload (ADR-0309);
    # it is never part of capsule.yaml.
    differing = tuple(
        sorted(
            k
            for k in (set(signed) | set(on_disk)) - {SEAL_METADATA_KEY}
            if signed.get(k) != on_disk.get(k)
        )
    )
    return True, not differing, differing, ()


def capsule_binding(capsule_dir: Path, dsse_bytes: bytes) -> CapsuleBinding:
    """Check that *capsule_dir* is the directory the seal was made over (ADR-0251).

    Never raises: an unreadable envelope degrades to an error line, an absent
    check to NOT PRESENT.
    """
    if not dsse_bytes:
        return CapsuleBinding()
    signed, error = _signed_manifest(dsse_bytes)
    if signed is None:
        return CapsuleBinding(errors=(error or "Cannot decode signed payload",))

    present, manifest_ok, differing, errors = _manifest_binding(capsule_dir, signed)
    digests = signed.get("evidence_digests")
    if not isinstance(digests, dict):  # sealed before ADR-0251 — NOT PRESENT
        return CapsuleBinding(
            manifest_present=present,
            manifest_ok=manifest_ok,
            differing_keys=differing,
            errors=errors,
        )

    on_disk_files = {
        path.relative_to(capsule_dir).as_posix()
        for path in capsule_dir.rglob("*")
        if path.is_file()
        and not path.is_symlink()
        and path.relative_to(capsule_dir).parts[0] not in {".seal", "capsule.yaml"}
    }
    mismatched: list[str] = []
    missing: list[str] = []
    for rel, expected in sorted(digests.items()):
        try:
            actual = _file_digest(capsule_dir, rel)
        except FileNotFoundError:
            missing.append(rel)
            continue
        except (OSError, ValueError, CapsuleEntryError):
            # A link, special file or unsafe name is never followed: whatever it
            # points at is not the bytes that were signed.
            mismatched.append(rel)
            continue
        if not isinstance(expected, dict) or actual != expected.get("sha256"):
            mismatched.append(rel)
    return CapsuleBinding(
        manifest_present=present,
        manifest_ok=manifest_ok,
        differing_keys=differing,
        digests_present=True,
        digests_ok=not mismatched and not missing,
        mismatched=tuple(mismatched),
        missing=tuple(missing),
        # Files added after sealing are legitimate (`nova export-c2pa` writes
        # one): reported, never fatal (ADR-0251 §5).
        unlisted=tuple(sorted(on_disk_files - set(digests))),
        errors=errors,
    )


def _file_digest(capsule_dir: Path, rel: str) -> str:
    """``sha256:<hex>`` of capsule file *rel*, streamed, following no link."""
    digest = hashlib.sha256()
    with open_capsule_file(capsule_dir, rel, operation=_OPERATION) as handle:
        for chunk in iter(lambda: handle.read(_HASH_CHUNK), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def read_capsule_tsr(tsr_path: Path) -> bytes | None:
    """Read ``manifest.dsse.tsr`` bounded to one byte past the token size limit.

    ``None`` when the file is absent. An oversize file is returned truncated to
    ``MAX_TOKEN_BYTES + 1`` bytes, which the parser then rejects (fail closed).
    """
    from novafabric.trust.novaseal.tsa_token import MAX_TOKEN_BYTES  # noqa: PLC0415

    try:
        return read_capsule_file(
            tsr_path.parent, tsr_path.name, operation=_OPERATION, limit=MAX_TOKEN_BYTES + 1
        )
    except FileNotFoundError:
        return None
    except CapsuleEntryError:
        return b""  # a linked or special token is never followed: it fails as empty


def _anchor_by_fingerprint(anchors: list[Any], fingerprint: str | None) -> Any:
    from cryptography.hazmat.primitives import hashes  # noqa: PLC0415

    if fingerprint is None:
        return None
    for cert in anchors:
        if "sha256:" + cert.fingerprint(hashes.SHA256()).hex() == fingerprint:
            return cert
    return None


def signer_chain_check(
    dsse_bytes: bytes,
    bundle_path: Path,
    *,
    crl_dir: Path | None = None,
    crl_strict: bool = False,
) -> AnchorCheck:
    """Bind the DSSE signature to a CA-validated signer certificate (ADR-0055).

    Offline and fail-closed: passes only when a signature entry verifies under
    its own embedded certificate *and* that certificate chains to the bundle.
    With *crl_dir* (ADR-0070 §3) the path is revocation-checked against local
    CRLs, never fetched; an unusable CRL directory fails closed.
    """
    from novafabric.trust.novaseal.crl import (  # noqa: PLC0415
        CrlStoreError,
        load_crl_directory,
    )
    from novafabric.trust.novaseal.x509_identity import (  # noqa: PLC0415
        X509ChainError,
        load_ca_bundle,
        verify_dsse_signer_chain,
    )

    try:
        anchors = load_ca_bundle(bundle_path.read_bytes())
        store = load_crl_directory(crl_dir) if crl_dir is not None else None
        outcome = verify_dsse_signer_chain(
            dsse_bytes, anchors, crl_store=store, crl_strict=crl_strict
        )
    except OSError as exc:
        return AnchorCheck(False, f"cannot read CA bundle {bundle_path}: {exc}")
    except (X509ChainError, CrlStoreError) as exc:
        return AnchorCheck(False, str(exc))
    if not outcome.valid:
        return AnchorCheck(False, outcome.reason, revocation=outcome.revocation)
    return AnchorCheck(
        True,
        "chain: " + " <- ".join(outcome.chain_subjects),
        revocation=outcome.revocation,
        anchor=_anchor_by_fingerprint(anchors, outcome.trust_anchor_fingerprint),
        chain_subjects=tuple(outcome.chain_subjects),
    )


def tsa_chain_check(
    token: bytes | None,
    expected_digest: bytes,
    anchor_paths: list[Path],
    *,
    crl_dir: Path | None = None,
    crl_strict: bool = False,
) -> AnchorCheck:
    """Verify an RFC 3161 token's TSA signature, EKU and chain (ADR-0070 §1).

    Only called when TSA anchors were given or configured: the operator then
    asked for a TSA-attested time, so an absent or empty token *fails* — the
    ``.tsr`` is outside the DSSE signature and a deleted one would otherwise be
    indistinguishable from one never requested.
    """
    from novafabric.trust.novaseal.tsa_trust import verify_tsa_trust_chain  # noqa: PLC0415

    try:
        anchors_pem = b"\n".join(p.read_bytes() for p in anchor_paths)
    except OSError as exc:
        return AnchorCheck(False, f"cannot read TSA CA bundle: {exc}")
    if token is None:
        return AnchorCheck(False, _NO_TOKEN_REASON)
    if not token:
        return AnchorCheck(
            False,
            "timestamp token (manifest.dsse.tsr) is empty although TSA trust anchors "
            "are configured",
        )
    outcome = verify_tsa_trust_chain(
        token, anchors_pem, crl_dir, crl_strict=crl_strict, expected_digest=expected_digest
    )
    return AnchorCheck(
        outcome.valid,
        outcome.reason,
        revocation=outcome.revocation,
        gen_time=outcome.gen_time.isoformat() if outcome.gen_time is not None else None,
        signer_subject=outcome.signer_subject,
        chain_subjects=tuple(outcome.chain_subjects),
    )


def _open_logs(profile: Any) -> tuple[Any, list[Any]]:
    """The configured Merkle log and ``previous_merkle_dbs``, only if they exist."""
    from novafabric.trust.novaseal.merkle import open_existing_merkle_log  # noqa: PLC0415

    if profile is None:
        return None, []
    current = open_existing_merkle_log(getattr(profile, "merkle_db", None))
    previous = []
    for location in getattr(profile, "previous_merkle_dbs", None) or []:
        opened = open_existing_merkle_log(location)
        if opened is not None:
            previous.append(opened)
    return current, previous


def _tsa_anchor_paths(profile: Any, tsa_ca_bundle: Path | None) -> list[Path]:
    if tsa_ca_bundle is not None:
        return [tsa_ca_bundle]
    configured = getattr(profile, "tsa_ca_certs", None)
    return [Path(p) for p in configured] if isinstance(configured, list) else []


def verify_capsule(
    capsule_dir: Path,
    profile: Any = None,
    *,
    ca_bundle: Path | None = None,
    crl_dir: Path | None = None,
    crl_strict: bool = False,
    tsa_ca_bundle: Path | None = None,
) -> CapsuleVerification:
    """Verify the sealed capsule at *capsule_dir* the way ``nova verify`` does.

    *profile* is the loaded ``novaseal.yaml`` (or None — verification is then
    from the capsule alone, ADR-0297 S2). Explicit arguments override the
    profile's ``ca_bundle`` / ``crl_dir`` / ``crl_strict`` / ``tsa_ca_certs``.
    Never creates a log. Raises nothing for a malformed capsule; every failure
    is in the returned report.
    """
    from novafabric.trust.novaseal.identity_trust import (  # noqa: PLC0415
        anchored_identity_trust,
    )

    seal_dir = capsule_dir / ".seal"
    read_errors: list[str] = []
    try:
        dsse_bytes = read_capsule_file(
            capsule_dir,
            ".seal/manifest.dsse",
            operation=_OPERATION,
            max_bytes=_MAX_ENVELOPE_BYTES,
        )
    except FileNotFoundError:
        dsse_bytes = b""
    except CapsuleEntryError as exc:
        dsse_bytes = b""
        read_errors.append(f"manifest.dsse not read: {exc}")

    merkle_log, previous_logs = _open_logs(profile)
    try:
        result = verify_seal_dir(seal_dir, merkle_log=merkle_log, previous_logs=previous_logs)
    finally:
        for opened in (merkle_log, *previous_logs):
            if opened is not None:
                opened.close()

    chain_bundle = ca_bundle if ca_bundle is not None else getattr(profile, "ca_bundle", None)
    crl_directory = crl_dir if crl_dir is not None else getattr(profile, "crl_dir", None)
    strict_crl = crl_strict or bool(getattr(profile, "crl_strict", False))
    chain_check = (
        signer_chain_check(
            dsse_bytes, Path(chain_bundle), crl_dir=crl_directory, crl_strict=strict_crl
        )
        if chain_bundle is not None
        else None
    )
    tsa_paths = _tsa_anchor_paths(profile, tsa_ca_bundle)
    tsa_check = (
        tsa_chain_check(
            read_capsule_tsr(seal_dir / "manifest.dsse.tsr"),
            hashlib.sha256(dsse_bytes).digest(),
            tsa_paths,
            crl_dir=crl_directory,
            crl_strict=strict_crl,
        )
        if tsa_paths
        else None
    )

    # ADR-0301: the signer trust level is what *this verifier* established.
    result.ca_chain_ok = chain_check is not None and chain_check.ok
    if result.signature_ok and chain_check is not None and chain_check.ok:
        result.identity_trust = anchored_identity_trust(chain_check.anchor)
    # Only a TSA chain that reached a verifier anchor makes the time trusted.
    result.timestamp_trust = timestamp_trust_for(
        result.timestamp_ok, tsa_check.ok if tsa_check is not None else None
    )

    return CapsuleVerification(
        capsule_dir=capsule_dir,
        result=result,
        binding=capsule_binding(capsule_dir, dsse_bytes),
        log_entry_capsule_id=_log_entry_capsule_id(capsule_dir),
        derived_capsule_id=derive_capsule_id(dsse_bytes),
        chain_check=chain_check,
        tsa_check=tsa_check,
        errors=read_errors,
    )
