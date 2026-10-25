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
import tempfile
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
    DEFAULT_MAX_MEMBERS,
    DEFAULT_MAX_UNCOMPRESSED_BYTES,
    BundleArchiveError,
    BundleTooLargeError,
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
    "MAX_SNAPSHOT_BYTES",
    "evidence_trust_anchors",
    "iter_file_chunks",
    "open_bundle",
    "snapshot_bundle",
    "verify_bundle_file",
    "verify_bundle_handle",
]

_log = logging.getLogger(__name__)

#: ``nova serve`` holds a bundle's capsule copy in memory to recompute its root, so
#: a request is bounded, by the same defaults ``nova verify`` applies.
MAX_VERIFY_UNCOMPRESSED_BYTES: Final[int] = DEFAULT_MAX_UNCOMPRESSED_BYTES
MAX_VERIFY_MEMBERS: Final[int] = DEFAULT_MAX_MEMBERS

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
            # ADR-0011 Amendment 7: a legacy bundle can be valid with unsigned members.
            "members_signed": o.members_signed,
            "unsigned_members": list(o.unsigned_members),
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
    #: Fingerprints (``sha256:`` of the SPKI DER) of the keys whose DSSE signatures
    #: verified — the key that actually signed, never a path the bundle names.
    signer_fingerprints: tuple[str, ...] = ()
    #: Every member covered by a verified members statement (ADR-0011 Amendment 7).
    members_signed: bool = False
    unsigned_members: tuple[str, ...] = ()
    #: ``created_at`` from the manifest of the very bytes that were verified.
    created_at: str | None = None

    @property
    def run_id(self) -> str | None:
        """The one signed run, or None."""
        return self.run_ids[0] if len(self.run_ids) == 1 else None


#: A cached verdict is valid for exactly the bytes it was computed over: the key is
#: the SHA-256 of the archive (plus the trust anchors). File identity (inode, size,
#: mtime, ctime) is not enough: on a filesystem with coarse timestamps (Lustre and
#: others keep 1 s) an in-place same-size rewrite with the mtime restored inside one
#: ctime tick left the identity unchanged and reused a stale ``verified: true``
#: (review r8).
_VerdictKey = tuple[str, str]

#: Largest archive a list/detail verdict snapshots and verifies; the verifier's own
#: uncompressed bound is the same, and a ZIP is never larger than what it inflates to
#: by much.
MAX_SNAPSHOT_BYTES: Final[int] = MAX_VERIFY_UNCOMPRESSED_BYTES

_COPY_CHUNK: Final[int] = 1024 * 1024


def _hash_stream(handle: BinaryIO) -> str:
    """SHA-256 of *handle* from offset 0 (bounded by :data:`MAX_SNAPSHOT_BYTES`)."""
    handle.seek(0)
    digest = hashlib.sha256()
    seen = 0
    while chunk := handle.read(_COPY_CHUNK):
        seen += len(chunk)
        if seen > MAX_SNAPSHOT_BYTES:
            raise BundleTooLargeError(f"the archive is larger than {MAX_SNAPSHOT_BYTES} bytes")
        digest.update(chunk)
    return digest.hexdigest()


def snapshot_bundle(handle: BinaryIO) -> tuple[BinaryIO, str]:
    """Copy *handle* into a private anonymous temporary file; return it and its SHA-256.

    Everything a verdict covers and everything shown next to it is read from this one
    copy, so a bundle rewritten in place while it is verified can never put forged
    content beside ``verified: true``. The caller closes the copy (it has no name).

    Raises:
        BundleTooLargeError: the archive is larger than :data:`MAX_SNAPSHOT_BYTES`.
    """
    handle.seek(0)
    snapshot = tempfile.TemporaryFile()  # noqa: SIM115 — returned to the caller
    digest = hashlib.sha256()
    copied = 0
    try:
        while chunk := handle.read(_COPY_CHUNK):
            copied += len(chunk)
            if copied > MAX_SNAPSHOT_BYTES:
                raise BundleTooLargeError(
                    f"the archive is larger than {MAX_SNAPSHOT_BYTES} bytes"
                )
            digest.update(chunk)
            snapshot.write(chunk)
        snapshot.seek(0)
    except BaseException:
        snapshot.close()
        raise
    return snapshot, digest.hexdigest()


class BundleVerdictCache:
    """Real-verifier verdicts for ``GET /api/evidence`` and ``/api/evidence/{id}``.

    Verifying a typical bundle costs a few milliseconds (3.6 ms measured on a
    75 KB single-capsule bundle), too much to repeat for up to 500 bundles on every
    list call but cheap once per content. A verdict is cached under the SHA-256 of
    the archive bytes it was computed over and the trust anchors, so any rewrite or
    re-anchoring is verified again; hashing is the only per-call cost. On a miss the
    archive is first copied into a private snapshot (hashed while copying) and the
    verifier reads only that copy, so the key always names the verified bytes.
    Bounded (LRU) and thread-safe; one per app, never module-global. ``POST
    …/verify`` never uses it.
    """

    def __init__(self, max_entries: int = 2048) -> None:
        self._max = max_entries
        self._entries: OrderedDict[_VerdictKey, BundleVerdict] = OrderedDict()
        self._lock = threading.Lock()

    def _lookup(self, key: _VerdictKey) -> BundleVerdict | None:
        with self._lock:
            cached = self._entries.get(key)
            if cached is not None:
                self._entries.move_to_end(key)
            return cached

    def _store(self, key: _VerdictKey, verdict: BundleVerdict) -> None:
        with self._lock:
            self._entries[key] = verdict
            self._entries.move_to_end(key)
            while len(self._entries) > self._max:
                self._entries.popitem(last=False)

    def verdict(
        self,
        handle: BinaryIO,
        bundle_id: str,
        *,
        trusted_public_pems: Sequence[bytes],
        tsa_chain_check: TsaChainCheck,
    ) -> BundleVerdict:
        """The verdict for the bundle open as *handle* (verified on a cache miss)."""
        anchors = _anchors_key(trusted_public_pems)
        try:
            cached = self._lookup((_hash_stream(handle), anchors))
        except BundleTooLargeError as exc:
            return BundleVerdict(False, reason=str(exc)[:300])
        if cached is not None:
            return cached
        try:
            snapshot, digest = snapshot_bundle(handle)
        except BundleTooLargeError as exc:
            return BundleVerdict(False, reason=str(exc)[:300])
        with snapshot:
            return self._verify_snapshot(
                snapshot, (digest, anchors), trusted_public_pems, tsa_chain_check
            )

    def snapshot_verdict(
        self,
        handle: BinaryIO,
        bundle_id: str,
        *,
        trusted_public_pems: Sequence[bytes],
        tsa_chain_check: TsaChainCheck,
    ) -> tuple[BundleVerdict, BinaryIO]:
        """The verdict and the private snapshot it covers; the caller closes the snapshot.

        For the detail route: what it shows next to the verdict must be read from the
        returned snapshot, never from *handle* again.

        Raises:
            BundleTooLargeError: the archive is larger than :data:`MAX_SNAPSHOT_BYTES`.
        """
        snapshot, digest = snapshot_bundle(handle)
        try:
            key = (digest, _anchors_key(trusted_public_pems))
            verdict = self._lookup(key) or self._verify_snapshot(
                snapshot, key, trusted_public_pems, tsa_chain_check
            )
            snapshot.seek(0)
        except BaseException:
            snapshot.close()
            raise
        return verdict, snapshot

    def _verify_snapshot(
        self,
        snapshot: BinaryIO,
        key: _VerdictKey,
        trusted_public_pems: Sequence[bytes],
        tsa_chain_check: TsaChainCheck,
    ) -> BundleVerdict:
        verdict = _compute_verdict(snapshot, trusted_public_pems, tsa_chain_check)
        self._store(key, verdict)
        return verdict


def _anchors_key(trusted_public_pems: Sequence[bytes]) -> str:
    return hashlib.sha256(b"\0".join(trusted_public_pems)).hexdigest()


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
    o = check.outcome
    created = o.manifest.get("created_at") or o.manifest.get("timestamp")
    return BundleVerdict(
        check.ok,
        tuple(check.run_ids),
        o.signer_trust,
        signer_fingerprints=tuple(o.signatures.signer_fingerprints),
        members_signed=o.members_signed,
        unsigned_members=tuple(o.unsigned_members),
        created_at=created if isinstance(created, str) else None,
    )
