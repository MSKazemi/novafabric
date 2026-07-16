"""Offline verification of a backup set against its manifest (ADR-0181).

``verify_backup`` recomputes every member's SHA-256 against the manifest and,
when the set carries a DSSE envelope, verifies the manifest signature. It needs
no live deployment, no network, and no private keys. Per the spec, a single
member mismatch is a **verification failure of the whole set**, never a
warning; unknown manifest keys are rejected (closed schema); and a set found to
contain key material is an invalid set.
"""

from __future__ import annotations

import hashlib
import json
import tarfile
from pathlib import Path

from pydantic import ValidationError

from novafabric.backup.create import MANIFEST_DSSE_NAME, MANIFEST_NAME, is_denied
from novafabric.backup.models import BackupManifest, MemberCheck, VerifyResult
from novafabric.trust.novaseal.envelope import EnvelopeError, verify_envelope


class BackupVerifyError(Exception):
    """Raised when the archive or its manifest cannot be read at all."""


def verify_backup(path: Path) -> VerifyResult:
    """Verify the backup set at *path* (a ``.tar.gz`` archive) offline.

    Returns a :class:`VerifyResult` listing ok / mismatched / missing members.
    ``ok`` is True only when every member hash matches, no normative-exclusion
    violation is found, and — for a signed set — the DSSE signature verifies.

    Raises:
        BackupVerifyError: when *path* is not a readable backup archive or its
            manifest is missing/malformed (including unknown manifest keys).
    """
    if not path.exists():
        raise BackupVerifyError(f"Backup set not found: {path}")

    try:
        with tarfile.open(path, "r:gz") as tar:
            manifest = _read_manifest(tar, path)
            errors: list[str] = []
            checks = [_check_member(tar, m.path, m.sha256) for m in manifest.members]

            for member in manifest.members:
                if is_denied(member.path):
                    errors.append(
                        f"Set contains excluded path {member.path!r} — key material or "
                        "secrets must never appear in a backup set (invalid set)"
                    )

            signature_verified = _verify_signature(tar, manifest, errors)
    except tarfile.TarError as exc:
        raise BackupVerifyError(f"Cannot read backup archive {path}: {exc}") from exc

    ok = (
        all(c.status == "ok" for c in checks)
        and not errors
        and signature_verified is not False
    )
    return VerifyResult(
        ok=ok,
        set_id=manifest.set_id,
        profile=manifest.profile,
        checks=checks,
        signing_status=manifest.signing_status,
        signature_verified=signature_verified,
        errors=errors,
    )


def _read_manifest(tar: tarfile.TarFile, path: Path) -> BackupManifest:
    raw = _extract_bytes(tar, MANIFEST_NAME)
    if raw is None:
        raise BackupVerifyError(f"Backup set {path} has no {MANIFEST_NAME}")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BackupVerifyError(f"{MANIFEST_NAME} is not valid JSON: {exc}") from exc
    try:
        # Closed schema: BackupManifest forbids unknown keys, per the spec.
        return BackupManifest.model_validate(data)
    except ValidationError as exc:
        raise BackupVerifyError(
            f"{MANIFEST_NAME} does not match the manifest schema: {exc}"
        ) from exc


def _check_member(tar: tarfile.TarFile, arc_path: str, expected_sha256: str) -> MemberCheck:
    digest = hashlib.sha256()
    try:
        fh = tar.extractfile(arc_path)
    except KeyError:
        fh = None
    if fh is None:
        return MemberCheck(path=arc_path, status="missing", expected_sha256=expected_sha256)
    with fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    actual = digest.hexdigest()
    status = "ok" if actual == expected_sha256 else "mismatch"
    return MemberCheck(
        path=arc_path,
        status=status,  # type: ignore[arg-type]
        expected_sha256=expected_sha256,
        actual_sha256=actual,
    )


def _verify_signature(
    tar: tarfile.TarFile, manifest: BackupManifest, errors: list[str]
) -> bool | None:
    """Verify the DSSE envelope when present. None means honestly unsigned."""
    dsse_bytes = _extract_bytes(tar, MANIFEST_DSSE_NAME)

    if manifest.signing_status == "unsigned":
        if dsse_bytes is not None:
            errors.append(
                f"Manifest says unsigned but the set contains {MANIFEST_DSSE_NAME}"
            )
            return False
        return None

    if dsse_bytes is None:
        errors.append(
            f"Manifest says signed but the set has no {MANIFEST_DSSE_NAME}"
        )
        return False

    try:
        verify_envelope(dsse_bytes, expected_payload=manifest.signable_bytes())
    except EnvelopeError as exc:
        errors.append(f"Manifest DSSE signature verification failed: {exc}")
        return False
    return True


def _extract_bytes(tar: tarfile.TarFile, name: str) -> bytes | None:
    try:
        fh = tar.extractfile(name)
    except KeyError:
        return None
    if fh is None:
        return None
    with fh:
        return fh.read()
