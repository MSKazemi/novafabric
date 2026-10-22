"""Evidence Bundle verification for ``POST /api/evidence/{bundle_id}/verify``.

The route used to check only the run attestation's DSSE signature, under the
certificate the bundle itself carries, plus the RFC 3161 token. It never recomputed
``manifest_hash``, an artifact digest or a signed subject, so a bundle with an edited
artifact, an edited manifest, swapped subjects or a wholesale attacker re-signing
reported ``valid: true`` (M11). The route now runs the same verifier as
``nova verify <bundle>.zip`` (:func:`novafabric.evidence.bundle_verify.verify_bundle_archive`),
so the dashboard and the CLI reach the same verdict on the same bytes.

The key that checks the signatures ships inside the bundle, so on its own a pass
proves consistency, not origin. The operator's trust list
(:func:`novafabric._paths.evidence_trusted_keys_path`) is the only anchor: a key in
the request is never used. Without one the response says ``signer_trust:
"unanchored"``; with one, a bundle signed by any other key is ``valid: false``.
"""

from __future__ import annotations

import logging
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from novafabric import _paths as _nf_paths
from novafabric._capsule_fs import open_capsule_file
from novafabric.evidence.bundle_verify import (
    BundleVerification,
    load_trusted_public_keys,
    verify_bundle_archive,
)

__all__ = [
    "MAX_VERIFY_MEMBERS",
    "MAX_VERIFY_UNCOMPRESSED_BYTES",
    "BundleCheck",
    "evidence_trust_anchors",
    "verify_bundle_file",
]

_log = logging.getLogger(__name__)

#: ``nova serve`` holds a bundle's capsule copy in memory to recompute its root, so
#: a request is bounded; ``nova verify`` on the command line has no such bound.
MAX_VERIFY_UNCOMPRESSED_BYTES: Final[int] = 1024 * 1024 * 1024
MAX_VERIFY_MEMBERS: Final[int] = 100_000

_RUN_ENVELOPE: Final[str] = "attestations/run.intoto.json"
_TOKEN: Final[str] = "manifest.dsse.tsr"

#: ``(token, timestamped bytes) -> True | False | None`` — the TSA trust-chain check
#: against the operator's ``tsa_ca_certs``; ``None`` when no anchor is configured.
TsaChainCheck = Callable[[bytes, bytes], "bool | None"]


@dataclass
class BundleCheck:
    """The bundle-level part of the route's verdict (the local seal is added by the route)."""

    outcome: BundleVerification
    timestamp_ok: bool | None = None
    tsa_chain_ok: bool | None = None
    timestamp_errors: list[str] = field(default_factory=list)

    @property
    def run_id(self) -> str | None:
        """The run the bundle's signature vouches for; else the manifest's unsigned label.

        ``manifest.json`` is not signed, so its ``subject.run_id`` (or a legacy top-level
        ``run_id``) is used only when no run statement verified. The route checks the
        local NovaSeal log of the capsule with this id.
        """
        signed = self.outcome.signatures.signed_run_ids
        if len(signed) == 1:
            return signed[0]
        subject = self.outcome.manifest.get("subject")
        for label in (
            subject.get("run_id") if isinstance(subject, dict) else None,
            self.outcome.manifest.get("run_id"),
        ):
            if isinstance(label, str) and label:
                return label
        return None

    @property
    def ok(self) -> bool:
        return self.outcome.ok and self.timestamp_ok is not False and self.tsa_chain_ok is not False

    def fields(self) -> dict[str, Any]:
        """The additive response fields; ``valid``/``errors`` are assembled by the route."""
        o = self.outcome
        return {
            "signature_ok": o.signature_ok,
            "manifest_hash_ok": o.signatures.manifest_hash_ok,
            "artifacts_ok": o.digests_ok,
            "complete": o.complete,
            "no_extras": o.no_extras,
            "subjects_ok": not o.signatures.bad_subjects,
            "attestations_verified": o.signatures.attestations_checked,
            "signer_trust": o.signer_trust,
            "signer_fingerprints": list(o.signatures.signer_fingerprints),
            "timestamp_ok": self.timestamp_ok,
        }


def evidence_trust_anchors() -> list[bytes]:
    """The operator's trusted evidence-signer public keys; empty when none is configured.

    Raises:
        TrustedKeysError: ``NOVAFABRIC_EVIDENCE_TRUSTED_KEYS`` names a file that cannot
            be used, or the default file exists but is unusable. A broken anchor is
            never treated as "no anchor".
    """
    path, explicit = _nf_paths.evidence_trusted_keys_path()
    if not explicit and not (path.exists() or path.is_symlink()):
        return []
    return load_trusted_public_keys(path)


def _read_bounded(zf: zipfile.ZipFile, name: str, limit: int) -> bytes:
    with zf.open(name) as handle:
        return handle.read(limit + 1)


def _check_timestamp(
    zf: zipfile.ZipFile, manifest: dict[str, Any], tsa_chain_check: TsaChainCheck
) -> tuple[bool | None, bool | None, list[str]]:
    """``(timestamp_ok, tsa_chain_ok, errors)`` for ``manifest.dsse.tsr``.

    The token timestamps ``attestations/run.intoto.json`` (what ``nova export
    evidence`` stamps). An empty token means the TSA was skipped: absent, not passed.
    """
    from novafabric.trust.novaseal.timestamp import verify_timestamp  # noqa: PLC0415
    from novafabric.trust.novaseal.tsa_token import MAX_TOKEN_BYTES  # noqa: PLC0415

    names = set(zf.namelist())
    if _TOKEN not in names:
        if manifest.get("timestamp_status") == "failed":
            return False, None, ["Timestamp was not obtained during export (TSA error)"]
        return None, None, []
    token = _read_bounded(zf, _TOKEN, MAX_TOKEN_BYTES)
    if not token:
        return None, None, []
    if len(token) > MAX_TOKEN_BYTES:
        return False, None, [f"RFC 3161 token is larger than {MAX_TOKEN_BYTES} bytes"]
    if _RUN_ENVELOPE not in names:
        return False, None, ["Cannot verify TSR: DSSE envelope missing"]
    envelope = zf.read(_RUN_ENVELOPE)
    try:
        if not verify_timestamp(token, envelope):
            return False, None, ["RFC 3161 TSR verification failed: hash mismatch"]
        chain_ok = tsa_chain_check(token, envelope)
    except Exception as exc:  # noqa: BLE001 — a hostile token is reported, never raised
        return False, None, [f"TSR verification error: {type(exc).__name__}: {exc}"]
    if chain_ok is False:
        return True, False, ["RFC 3161 TSA chain does not reach tsa_ca_certs"]
    return True, chain_ok, []


def verify_bundle_file(
    evidence_dir: Path,
    bundle_id: str,
    *,
    trusted_public_pems: list[bytes],
    tsa_chain_check: TsaChainCheck,
) -> BundleCheck:
    """Verify ``<evidence_dir>/<bundle_id>.zip``; blocking, run it off the event loop.

    The archive is opened without following a link and must be a regular file
    (:func:`novafabric._capsule_fs.open_capsule_file`), so a planted symlink or FIFO is
    refused rather than read or waited on. *bundle_id* must already be a validated
    single path segment.

    Raises:
        FileNotFoundError: no such bundle (or no evidence directory).
        CapsuleEntryError: the bundle path is a link or not a regular file.
        zipfile.BadZipFile: not a ZIP archive.
        BundleArchiveError: no usable ``manifest.json``.
        BundleTooLargeError: above :data:`MAX_VERIFY_UNCOMPRESSED_BYTES` or
            :data:`MAX_VERIFY_MEMBERS`.
    """
    with (
        open_capsule_file(
            evidence_dir, f"{bundle_id}.zip", operation="nova serve evidence verify"
        ) as handle,
        zipfile.ZipFile(handle) as zf,
    ):
        outcome = verify_bundle_archive(
            zf,
            trusted_public_pems=trusted_public_pems,
            max_uncompressed_bytes=MAX_VERIFY_UNCOMPRESSED_BYTES,
            max_members=MAX_VERIFY_MEMBERS,
        )
        timestamp_ok, tsa_chain_ok, ts_errors = _check_timestamp(
            zf, outcome.manifest, tsa_chain_check
        )
    check = BundleCheck(outcome, timestamp_ok, tsa_chain_ok, ts_errors)
    _log.info(
        "evidence_bundle_verified",
        extra={
            "bundle_id": bundle_id,
            "ok": check.ok,
            "signer_trust": outcome.signer_trust,
            "problems": len(outcome.problems()) + len(ts_errors),
        },
    )
    return check
