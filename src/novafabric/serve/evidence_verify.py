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

import hashlib
import logging
import os
import threading
import zipfile
from collections import OrderedDict
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, Final

from novafabric import _paths as _nf_paths
from novafabric._capsule_fs import open_capsule_file
from novafabric.evidence.bundle_verify import (
    BundleArchiveError,
    BundleVerification,
    load_trusted_public_keys,
    verify_bundle_archive,
)

__all__ = [
    "MAX_VERIFY_MEMBERS",
    "MAX_VERIFY_UNCOMPRESSED_BYTES",
    "BundleCheck",
    "BundleVerdict",
    "BundleVerdictCache",
    "evidence_trust_anchors",
    "iter_file_chunks",
    "open_bundle",
    "verify_bundle_file",
    "verify_bundle_handle",
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
    def run_ids(self) -> list[str]:
        """Every run a verified run statement signs — the only runs the route reports.

        ``manifest.json`` is unsigned, so its ``subject`` labels and a legacy top-level
        ``run_id`` are never reported (attack A2, 2026-10-23: a set bundle's route
        verdict named the unsigned top-level ``run_id``).
        """
        return list(self.outcome.signatures.signed_run_ids)

    @property
    def run_id(self) -> str | None:
        """The one signed run, or None when no run — or more than one — verified."""
        signed = self.run_ids
        return signed[0] if len(signed) == 1 else None

    @property
    def ok(self) -> bool:
        return self.outcome.ok and self.timestamp_ok is not False and self.tsa_chain_ok is not False

    def fields(self) -> dict[str, Any]:
        """The additive response fields; ``valid``/``errors`` are assembled by the route."""
        o = self.outcome
        return {
            "run_ids": self.run_ids,
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


#: Chunk size the ``/download`` route streams a bundle in.
DOWNLOAD_CHUNK_BYTES: Final[int] = 1024 * 1024


def open_bundle(
    evidence_dir: Path, bundle_id: str, *, operation: str = "nova serve evidence"
) -> BinaryIO:
    """Open ``<evidence_dir>/<bundle_id>.zip`` for reading, following no link.

    The one way every Evidence Bundle route (list, detail, ``/download``,
    ``/verify``) reads a bundle: through
    :func:`novafabric._capsule_fs.open_capsule_file`, so a planted symlink is
    refused rather than followed and a FIFO is refused rather than waited on.
    *bundle_id* must already be a validated single path segment.

    Raises:
        FileNotFoundError: no such bundle (or no evidence directory).
        CapsuleEntryError: the bundle path is a link or not a regular file.
    """
    return open_capsule_file(evidence_dir, f"{bundle_id}.zip", operation=operation)


def iter_file_chunks(
    handle: BinaryIO, chunk_bytes: int = DOWNLOAD_CHUNK_BYTES
) -> Iterator[bytes]:
    """Yield *handle*'s bytes in bounded chunks, closing it when done or abandoned."""
    try:
        while chunk := handle.read(chunk_bytes):
            yield chunk
    finally:
        handle.close()


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
    with open_bundle(evidence_dir, bundle_id, operation="nova serve evidence verify") as handle:
        check = verify_bundle_handle(
            handle, trusted_public_pems=trusted_public_pems, tsa_chain_check=tsa_chain_check
        )
    _log.info(
        "evidence_bundle_verified",
        extra={
            "bundle_id": bundle_id,
            "ok": check.ok,
            "signer_trust": check.outcome.signer_trust,
            "problems": len(check.outcome.problems()) + len(check.timestamp_errors),
        },
    )
    return check


def verify_bundle_handle(
    handle: BinaryIO,
    *,
    trusted_public_pems: Sequence[bytes],
    tsa_chain_check: TsaChainCheck,
) -> BundleCheck:
    """Verify an already-opened bundle file (see :func:`verify_bundle_file` for errors)."""
    with zipfile.ZipFile(handle) as zf:
        outcome = verify_bundle_archive(
            zf,
            trusted_public_pems=trusted_public_pems,
            max_uncompressed_bytes=MAX_VERIFY_UNCOMPRESSED_BYTES,
            max_members=MAX_VERIFY_MEMBERS,
        )
        timestamp_ok, tsa_chain_ok, ts_errors = _check_timestamp(
            zf, outcome.manifest, tsa_chain_check
        )
    return BundleCheck(outcome, timestamp_ok, tsa_chain_ok, ts_errors)


@dataclass(frozen=True)
class BundleVerdict:
    """The shared verifier's verdict on one bundle, as the list and detail routes show it.

    ``verified`` is :attr:`BundleCheck.ok` — every recipe step, every signer in the
    operator trust list when one is configured, and the RFC 3161 token. ``reason``
    says why verification could not run (too large, not a ZIP, …); ``verified`` is
    then False.
    """

    verified: bool
    run_ids: tuple[str, ...] = ()
    signer_trust: str = "none"
    reason: str | None = None

    @property
    def run_id(self) -> str | None:
        """The one signed run, or None."""
        return self.run_ids[0] if len(self.run_ids) == 1 else None


#: The file identity a cached verdict is valid for. ``st_ctime_ns`` is in it because
#: anyone who can rewrite a bundle in place can also restore its mtime (``utime``),
#: but not its ctime.
_VerdictKey = tuple[str, int, int, int, int, int, str]


class BundleVerdictCache:
    """Real-verifier verdicts for ``GET /api/evidence`` and ``/api/evidence/{id}``.

    Verifying a typical bundle costs a few milliseconds (3.6 ms measured on a
    75 KB single-capsule bundle), too much to repeat for up to 500 bundles on every
    list call but cheap once per file state. A verdict is cached under the opened
    file's identity (device, inode, size, mtime, ctime) and the trust anchors, so a
    replaced, rewritten or re-anchored bundle is verified again. Bounded (LRU) and
    thread-safe; one per app, never module-global. ``POST …/verify`` never uses it.
    """

    def __init__(self, max_entries: int = 2048) -> None:
        self._max = max_entries
        self._entries: OrderedDict[_VerdictKey, BundleVerdict] = OrderedDict()
        self._lock = threading.Lock()

    def verdict(
        self,
        handle: BinaryIO,
        bundle_id: str,
        *,
        trusted_public_pems: Sequence[bytes],
        tsa_chain_check: TsaChainCheck,
    ) -> BundleVerdict:
        """The verdict for the bundle open as *handle* (verified on a cache miss)."""
        st = os.fstat(handle.fileno())
        anchors = hashlib.sha256(b"\0".join(trusted_public_pems)).hexdigest()
        key: _VerdictKey = (
            bundle_id, st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns, anchors
        )
        with self._lock:
            cached = self._entries.get(key)
            if cached is not None:
                self._entries.move_to_end(key)
                return cached
        verdict = _compute_verdict(handle, trusted_public_pems, tsa_chain_check)
        with self._lock:
            self._entries[key] = verdict
            self._entries.move_to_end(key)
            while len(self._entries) > self._max:
                self._entries.popitem(last=False)
        return verdict


def _compute_verdict(
    handle: BinaryIO, trusted_public_pems: Sequence[bytes], tsa_chain_check: TsaChainCheck
) -> BundleVerdict:
    handle.seek(0)
    try:
        check = verify_bundle_handle(
            handle, trusted_public_pems=trusted_public_pems, tsa_chain_check=tsa_chain_check
        )
    except (zipfile.BadZipFile, BundleArchiveError, OSError, EOFError, ValueError) as exc:
        return BundleVerdict(False, reason=f"{type(exc).__name__}: {exc}"[:300])
    return BundleVerdict(check.ok, tuple(check.run_ids), check.outcome.signer_trust)
