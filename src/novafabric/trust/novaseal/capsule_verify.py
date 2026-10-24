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
import logging
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from novafabric._capsule_fs import (
    CapsuleEntryError,
    find_capsule_special_entries,
    open_capsule_file,
    read_capsule_file,
)
from novafabric._capsule_manifest import (
    MANIFEST_NAMES,
    parse_manifest_bytes,
    sealed_manifest_name,
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
    "unsealed_substitutes",
    "verify_capsule",
]

_OPERATION = "nova verify"

logger = logging.getLogger(__name__)
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
    run (no manifest file and none named by the seal; sealed before
    ``evidence_digests``) — reported as NOT PRESENT, never as passed and never as
    failed (ADR-0251 §4).

    ``manifest_name`` is the manifest file the seal was made over (or, when the
    seal names none, the one compared). ``unsealed_manifests`` are other
    manifest-named files (``capsule.yaml`` / ``capsule.json``) that disagree with
    the signed payload, and ``unsealed_refs`` files a signed ``*_ref`` names that
    the seal does not cover: readers take both as capsule content, so each fails —
    unlike ``unlisted`` derived artifacts, which are reported only (§5).
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
    manifest_name: str = "capsule.yaml"
    unsealed_manifests: tuple[str, ...] = ()
    unsealed_refs: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        """False when any check that ran failed; absent checks do not fail."""
        manifest = self.manifest_ok or not self.manifest_present
        digests = self.digests_ok or not self.digests_present
        return (
            manifest
            and digests
            and not self.errors
            and not self.unsealed_manifests
            and not self.unsealed_refs
        )

    def failure_reasons(self) -> list[str]:
        """One human-readable line per failed binding check."""
        reasons = list(self.errors)
        if self.manifest_present and not self.manifest_ok:
            if self.differing_keys:
                reasons.append(
                    f"{self.manifest_name} disagrees with the signed payload: "
                    + ", ".join(self.differing_keys)
                )
            elif not self.errors:
                reasons.append(f"{self.manifest_name} does not match the signed payload")
        reasons.extend(self.reasons)
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
    # ``metadata.capture_tampered`` from the *signed* manifest: the recorded
    # workload replaced capsule paths with links or special files during
    # capture. The seal still binds the capsule (it signs that record), but the
    # evidence is incomplete, so the capsule does not verify.
    capture_tampered: str | None = None
    # Symlinks, FIFOs, sockets and devices found anywhere in the directory now,
    # as ``"<rel> (<kind>)"``. NovaFabric never writes one, so each was planted
    # (during capture past the sweep, or after sealing). None is followed; each
    # fails the verification.
    special_entries: tuple[str, ...] = ()

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
            and self.capture_tampered is None
            and not self.special_entries
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
        if self.capture_tampered is not None:
            errors.append(capture_tampered_error(self.capture_tampered))
        errors.extend(special_entry_errors(self.special_entries))
        return errors


def capture_tampered_error(summary: str) -> str:
    """The verification error for a capsule whose capture recorded tampering."""
    return (
        "capture recorded tampering by the workload (metadata.capture_tampered: "
        f"{summary}); the paths were never followed, but the evidence is incomplete"
    )


#: Most planted entries named one by one; the count is always given.
_MAX_SPECIAL_REPORTED = 20


def special_entry_errors(entries: tuple[str, ...]) -> list[str]:
    """One verification error per planted link or special file (bounded)."""
    errors = [
        f"{entry} is inside the capsule: NovaFabric never writes a symlink, FIFO, "
        "socket or device, so it was planted (never followed)"
        for entry in entries[:_MAX_SPECIAL_REPORTED]
    ]
    if len(entries) > _MAX_SPECIAL_REPORTED:
        errors.append(
            f"and {len(entries) - _MAX_SPECIAL_REPORTED} more planted link(s) or "
            f"special file(s) ({len(entries)} in total)"
        )
    return errors


def capsule_special_entries(capsule_dir: Path) -> tuple[str, ...]:
    """Every symlink / FIFO / socket / device under *capsule_dir* as ``"<rel> (<kind>)"``."""
    return tuple(f"{rel} ({kind})" for rel, kind in find_capsule_special_entries(capsule_dir))


def signed_capture_tampered(dsse_bytes: bytes) -> str | None:
    """``metadata.capture_tampered`` of the signed manifest, or None."""
    if not dsse_bytes:
        return None
    signed, _error = _signed_manifest(dsse_bytes)
    metadata = signed.get("metadata") if signed is not None else None
    value = metadata.get("capture_tampered") if isinstance(metadata, dict) else None
    return str(value) if value else None


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


@dataclass(frozen=True)
class _ManifestCheck:
    """Outcome of comparing one manifest-named file with the signed payload."""

    name: str
    exists: bool
    differing: tuple[str, ...] = ()
    error: str | None = None

    @property
    def matches(self) -> bool:
        return self.exists and self.error is None and not self.differing


def _compare_manifest_file(capsule_dir: Path, name: str, signed: dict[str, Any]) -> _ManifestCheck:
    """Compare manifest file *name* structurally with the signed payload (ADR-0251 §2)."""
    if not _entry_exists(capsule_dir, name):
        return _ManifestCheck(name, exists=False)
    import yaml  # noqa: PLC0415

    try:
        # No link is followed: a linked manifest is reported, never compared with
        # the target's contents (R6).
        on_disk = parse_manifest_bytes(
            name,
            read_capsule_file(
                capsule_dir, name, operation=_OPERATION, max_bytes=_MAX_MANIFEST_BYTES
            ),
        )
    except (OSError, ValueError, yaml.YAMLError, CapsuleEntryError) as exc:
        return _ManifestCheck(name, exists=True, error=f"Cannot read {name}: {exc}")
    if not isinstance(on_disk, dict):
        if on_disk is None:
            return _ManifestCheck(name, exists=True, error=f"{name} is empty")
        return _ManifestCheck(name, exists=True, error=f"{name} did not parse as a mapping")
    # ``_novaseal`` is seal metadata bound into the signed payload (ADR-0309);
    # it is never part of a manifest file.
    differing = tuple(
        sorted(
            k
            for k in (set(signed) | set(on_disk)) - {SEAL_METADATA_KEY}
            if signed.get(k) != on_disk.get(k)
        )
    )
    return _ManifestCheck(name, exists=True, differing=differing)


def _entry_exists(capsule_dir: Path, rel: str) -> bool:
    path = capsule_dir / rel
    return path.exists() or path.is_symlink()


@dataclass(frozen=True)
class _ManifestBinding:
    present: bool = False
    ok: bool = False
    name: str = MANIFEST_NAMES[0]
    differing: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    unsealed: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    #: Manifest-named files the binding accounted for (never "unlisted").
    handled: frozenset[str] = frozenset()


def _manifest_binding(
    capsule_dir: Path, signed: dict[str, Any], digests: dict[str, Any] | None
) -> _ManifestBinding:
    """Bind the capsule's manifest files to the signed payload (ADR-0251 §2).

    The manifest the seal was made over (:func:`sealed_manifest_name`) must exist
    and equal the payload; missing, it fails. Any *other* manifest-named file must
    either be an evidence file the digest map covers or equal the payload too:
    readers accept ``capsule.json`` as a manifest, so an unsealed one that differs
    is a substituted manifest, never a harmless "unlisted" addition.
    """
    carrier = sealed_manifest_name(signed)
    checks = {name: _compare_manifest_file(capsule_dir, name, signed) for name in MANIFEST_NAMES}
    covered = set(digests) if digests else set()
    handled = frozenset(n for n, c in checks.items() if c.exists and n not in covered)

    if carrier is not None and carrier not in MANIFEST_NAMES:
        return _ManifestBinding(
            present=True,
            name=carrier,
            errors=(f"the seal names an unknown manifest file {carrier!r}",),
            handled=handled,
        )
    primary = carrier or next((n for n in MANIFEST_NAMES if checks[n].exists), None)
    unsealed: list[str] = []
    reasons: list[str] = []
    for name, check in checks.items():
        if name == primary or not check.exists or name in covered or check.matches:
            continue
        unsealed.append(name)
        detail = check.error or "disagrees with the signed payload: " + ", ".join(check.differing)
        reasons.append(f"{name} is a manifest file the seal does not cover ({detail})")

    if primary is None:
        # Not every sealed directory is a run capsule — the object capsule store
        # seals a single-field manifest with no file. Absent, not failed.
        return _ManifestBinding(handled=handled)
    check = checks[primary]
    if not check.exists:
        errors: tuple[str, ...] = (f"{primary} is missing: the seal was made over it",)
    else:
        errors = (check.error,) if check.error else ()
    return _ManifestBinding(
        present=True,
        ok=check.matches,
        name=primary,
        differing=check.differing,
        errors=errors,
        unsealed=tuple(unsealed),
        reasons=tuple(reasons),
        handled=handled,
    )


def _ref_target(value: object) -> str | None:
    """A signed ``*_ref`` value as a safe capsule-relative path, or None."""
    if not isinstance(value, str) or not value or "\\" in value:
        return None
    parts = PurePosixPath(value).parts
    if not parts or PurePosixPath(value).is_absolute() or ".." in parts or parts[0] == ".seal":
        return None
    return PurePosixPath(*parts).as_posix()


def _unsealed_refs(
    capsule_dir: Path, signed: dict[str, Any], digests: dict[str, Any]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Files a signed top-level ``*_ref`` names that exist but the seal does not cover.

    The signed manifest says "this capsule's tool calls are ``tool-calls.jsonl``".
    When no such file existed at seal time, one added later is not the evidence
    the signer vouched for, yet every reader would take it as that evidence.
    """
    unsealed: list[str] = []
    reasons: list[str] = []
    for key in sorted(signed):
        target = _ref_target(signed[key]) if key.endswith("_ref") else None
        if target is None or target in digests or target in MANIFEST_NAMES:
            continue
        if _entry_exists(capsule_dir, target) and target not in unsealed:
            unsealed.append(target)
            reasons.append(
                f"{target} is named by the signed manifest ({key}) but was not covered "
                "by the seal: it was added after sealing"
            )
    return tuple(unsealed), tuple(reasons)


def unsealed_substitutes(capsule_dir: Path, dsse_bytes: bytes) -> tuple[str, ...]:
    """Files a sealed capsule's readers would trust that its seal does not cover.

    The manifest-named files that disagree with the signed payload plus the
    targets of signed ``*_ref`` fields added after sealing — the two failure
    classes of :func:`capsule_binding` that substitute capsule content. Cheaper
    than the full binding: no evidence file is hashed. Empty for an envelope that
    does not decode (the full verifier reports that).
    """
    signed, _error = _signed_manifest(dsse_bytes)
    if signed is None:
        return ()
    raw = signed.get("evidence_digests")
    digests = raw if isinstance(raw, dict) else None
    manifests = _manifest_binding(capsule_dir, signed, digests).unsealed
    refs = _unsealed_refs(capsule_dir, signed, digests)[0] if digests is not None else ()
    return manifests + refs


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

    digests = signed.get("evidence_digests")
    manifest = _manifest_binding(
        capsule_dir, signed, digests if isinstance(digests, dict) else None
    )
    if not isinstance(digests, dict):  # sealed before ADR-0251 — NOT PRESENT
        return CapsuleBinding(
            manifest_present=manifest.present,
            manifest_ok=manifest.ok,
            differing_keys=manifest.differing,
            errors=manifest.errors,
            manifest_name=manifest.name,
            unsealed_manifests=manifest.unsealed,
            reasons=manifest.reasons,
        )

    unsealed_refs, ref_reasons = _unsealed_refs(capsule_dir, signed, digests)
    not_unlisted = {".seal", *manifest.handled, *unsealed_refs}
    on_disk_files = {
        rel
        for rel in (
            path.relative_to(capsule_dir).as_posix()
            for path in capsule_dir.rglob("*")
            if path.is_file() and not path.is_symlink()
        )
        if rel not in not_unlisted and rel.split("/", 1)[0] != ".seal"
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
        manifest_present=manifest.present,
        manifest_ok=manifest.ok,
        differing_keys=manifest.differing,
        digests_present=True,
        digests_ok=not mismatched and not missing,
        mismatched=tuple(mismatched),
        missing=tuple(missing),
        # Derived files added after sealing are legitimate (`nova export-c2pa`
        # writes one): reported, never fatal (ADR-0251 §5). A manifest-named file
        # or a signed ref's target is never one of them — those fail above.
        unlisted=tuple(sorted(on_disk_files - set(digests))),
        errors=manifest.errors,
        manifest_name=manifest.name,
        unsealed_manifests=manifest.unsealed,
        unsealed_refs=unsealed_refs,
        reasons=manifest.reasons + ref_reasons,
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
    from novafabric.trust.novaseal.log_location import is_postgres_dsn  # noqa: PLC0415
    from novafabric.trust.novaseal.merkle import open_existing_merkle_log  # noqa: PLC0415

    def _open(location: Any) -> Any:
        # A SQLite log is opened mode=ro: verification never migrates or writes
        # it (ADR-0297 S2). A Postgres log is opened as before.
        return open_existing_merkle_log(location, read_only=not is_postgres_dsn(location))

    if profile is None:
        return None, []
    current = _open(getattr(profile, "merkle_db", None))
    previous = []
    for location in getattr(profile, "previous_merkle_dbs", None) or []:
        opened = _open(location)
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
        capture_tampered=signed_capture_tampered(dsse_bytes),
        special_entries=capsule_special_entries(capsule_dir),
    )


def capsule_seal_verifies(capsule_dir: Path) -> bool:
    """True when *capsule_dir* carries a NovaSeal seal that verifies. Never raises.

    The one test every "is this capsule sealed?" evidence check uses: the
    ``.seal/`` signature verifies, the signed manifest binds the files on disk
    (``evidence_digests``) and the capsule id matches — :func:`verify_capsule`
    from the capsule alone, so the signer is self-asserted, never anchored.
    The audit engine, the evidence binding and the safety-case compiler used to
    accept a file named ``seal-bundle.json`` (which nothing writes) instead, so a
    hand-made file counted and a genuine seal did not (review cli-a-03).
    """
    try:
        if not (capsule_dir / ".seal").is_dir():
            return False
        return verify_capsule(capsule_dir).valid
    except Exception:  # noqa: BLE001 — evidence is absent, never a crash
        logger.debug("seal verification failed for %s", capsule_dir, exc_info=True)
        return False
