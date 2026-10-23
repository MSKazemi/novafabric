"""Signature, manifest-hash and subject checks for an Evidence Bundle.

Every bundle carries a verification recipe (``VERIFIER_INSTRUCTIONS`` in
:mod:`novafabric.evidence.bundle`) with five steps. ``nova verify <bundle>.zip``
used to perform only step 2 — recompute each artifact digest — so a bundle whose
signed attestation had been edited, with the edited file's digest written back into
``manifest.json``, verified as PASSED. This module performs steps 1, 3 and 4:

1. ``manifest_hash`` equals the canonical hash of the manifest without that field.
3. Every attestation in ``attestations[]`` has a ``signatures[]`` entry, its DSSE
   signature verifies under the key in that entry's ``certificate_path``, and the
   detached ``signature_path`` file is the envelope's own signature.
4. Each statement's subject digests equal the attestation's ``subject_hashes`` in the
   manifest, and a run-capsule statement's subject is one of the manifest's
   ``subject.capsule_hash`` values. The run subject is also recomputed from the
   bundle's own ``run-capsule/`` copy (v2 or v1 capsule root), and a lineage subject
   from its ``edges.jsonl``: ``manifest.json`` is not signed and its ``manifest_hash``
   is a self-hash anyone can recompute, so a subject compared only with the manifest
   binds nothing. Every file under ``run-capsule/`` must lie in a copy whose root a
   verified run statement signs.

The key that checks the signatures ships *inside* the bundle, so on its own a pass
proves the bundle is internally consistent and was signed as a whole — not who
signed it. Passing ``trusted_public_pem`` (``nova verify --public-key``) pins the
signer; without it the report names the key so the caller can compare it out of band.

:func:`verify_bundle_archive` runs the whole recipe (step 2's digests too) over an open
ZIP. It is the one verifier behind both ``nova verify <bundle>.zip`` and ``nova serve``'s
``POST /api/evidence/{bundle_id}/verify``, whose anchor is the operator trust list read by
:func:`load_trusted_public_keys` — so the CLI and the dashboard cannot disagree.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import stat
import zipfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from novafabric.evidence.bundle import (
    PREDICATE_LINEAGE,
    PREDICATE_RUN,
    _canonical_manifest_hash,
)
from novafabric.evidence.intoto import dsse_verify
from novafabric.evidence.merkle import ROOT_VERSIONS, root_over_files
from novafabric.evidence.signing import verify_with_pem


@dataclass
class BundleSignatureReport:
    """Outcome of the signature, manifest-hash and subject checks."""

    manifest_hash_ok: bool = False
    attestations_checked: int = 0
    unsigned: list[str] = field(default_factory=list)
    bad_signatures: list[str] = field(default_factory=list)
    bad_subjects: list[str] = field(default_factory=list)
    signer_fingerprints: list[str] = field(default_factory=list)
    signer_not_trusted: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    #: ``predicate.run_id`` of every run statement whose signature and subject verified.
    signed_run_ids: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return (
            self.manifest_hash_ok
            and self.attestations_checked > 0
            and not self.unsigned
            and not self.bad_signatures
            and not self.bad_subjects
            and not self.signer_not_trusted
            and not self.errors
        )


def _bare(digest: object) -> str:
    return str(digest or "").removeprefix("sha256:")


def _public_key_fingerprint(public_pem: bytes) -> str:
    key = serialization.load_pem_public_key(public_pem)
    raw = key.public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _capsule_hashes(manifest: dict[str, Any]) -> set[str]:
    subject = manifest.get("subject")
    subjects = subject if isinstance(subject, list) else [subject]
    return {
        _bare(s["capsule_hash"])
        for s in subjects
        if isinstance(s, dict) and isinstance(s.get("capsule_hash"), str) and s["capsule_hash"]
    }


def _subject_run_ids(manifest: dict[str, Any]) -> dict[str, object]:
    """Manifest ``subject`` entries' ``run_id`` labels, keyed by bare ``capsule_hash``."""
    subject = manifest.get("subject")
    subjects = subject if isinstance(subject, list) else [subject]
    return {
        _bare(s["capsule_hash"]): s["run_id"]
        for s in subjects
        if isinstance(s, dict) and isinstance(s.get("capsule_hash"), str) and "run_id" in s
    }


def _run_id_problem(statement: object, subject_run_ids: dict[str, object]) -> str:
    """A manifest subject labelled with a run id other than the one its run statement signs.

    ``manifest.json`` is not signed, so its ``subject.run_id`` is only a label; the run
    statement's ``predicate.run_id`` is what the signer vouched for. A relabelled subject
    would show a recipient (and ``nova serve``) the wrong run.
    """
    if not isinstance(statement, dict) or statement.get("predicateType") != PREDICATE_RUN:
        return ""
    signed = _predicate_run_id(statement)
    if signed is None:
        return ""
    for digest in _statement_digests(statement):
        label = subject_run_ids.get(digest)
        if label is not None and label != signed:
            return "manifest subject.run_id ≠ the run_id the run statement signs"
    return ""


def _as_list(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


def verify_bundle_signatures(
    manifest: dict[str, Any],
    read: Callable[[str], bytes],
    names: set[str],
    trusted_public_pem: bytes | None = None,
    *,
    trusted_public_pems: Sequence[bytes] = (),
) -> BundleSignatureReport:
    """Run recipe steps 1, 3 and 4 over a bundle's manifest and members.

    Every malformed or hostile input is reported in a field of the returned report;
    nothing in the manifest, an envelope, a certificate or a signed statement makes
    this function raise. The signing key ships inside the bundle, so an attacker can
    sign any statement — its shape is untrusted even when its signature verifies.

    Args:
        manifest: the parsed ``manifest.json``.
        read: returns the bytes of a bundle member by its path.
        names: every member path present in the bundle.
        trusted_public_pem: optional PEM public key the signer must match.
        trusted_public_pems: further PEM public keys (an operator trust list); with
            any key given, every attestation's signer must be one of them.
    """
    report = BundleSignatureReport()

    stored = manifest.get("manifest_hash")
    if not stored:
        report.errors.append("manifest.json has no manifest_hash")
    else:
        report.manifest_hash_ok = stored == _canonical_manifest_hash(manifest)

    raw_attestations = manifest.get("attestations")
    if not isinstance(raw_attestations, list):
        report.errors.append("manifest attestations is not a list")
        raw_attestations = []
    if not raw_attestations:
        report.errors.append("the bundle lists no attestations — nothing binds it to a signer")
    by_envelope = {
        s.get("envelope_path"): s
        for s in _as_list(manifest.get("signatures"))
        if isinstance(s, dict) and isinstance(s.get("envelope_path"), str)
    }
    anchors = ([trusted_public_pem] if trusted_public_pem else []) + list(trusted_public_pems)
    trusted_fps = {_public_key_fingerprint(pem) for pem in anchors} or None
    capsule_hashes = _capsule_hashes(manifest)
    subject_run_ids = _subject_run_ids(manifest)
    covered_copies: list[str] = []
    signed_runs: list[_SignedRun] = []

    for att in raw_attestations:
        if not isinstance(att, dict):
            report.errors.append(f"attestation entry is not an object: {att!r:.60}")
            continue
        path = att.get("path")
        if not isinstance(path, str):
            report.errors.append(f"attestation path is not a string: {path!r:.60}")
            continue
        sig = by_envelope.get(path)
        if sig is None:
            report.unsigned.append(path)
            continue
        cert_path = sig.get("certificate_path")
        detached = sig.get("signature_path")
        if path not in names or not isinstance(cert_path, str) or cert_path not in names:
            report.bad_signatures.append(f"{path} (envelope or certificate missing)")
            continue
        try:
            envelope = json.loads(read(path))
            cert = read(cert_path)
            statement = dsse_verify(envelope, lambda pae, s, c=cert: verify_with_pem(c, pae, s))
            if detached and (
                not isinstance(detached, str)
                or detached not in names
                or read(detached) != base64.b64decode(envelope["signatures"][0]["sig"])
            ):
                raise ValueError("detached signature differs from the envelope signature")
            fingerprint = _public_key_fingerprint(cert)
        except (
            ValueError, KeyError, IndexError, TypeError, AttributeError,
            binascii.Error, UnsupportedAlgorithm,
        ) as exc:
            report.bad_signatures.append(f"{path} ({type(exc).__name__}: {exc})")
            continue
        report.attestations_checked += 1
        if fingerprint not in report.signer_fingerprints:
            report.signer_fingerprints.append(fingerprint)
        if trusted_fps is not None and fingerprint not in trusted_fps:
            report.signer_not_trusted.append(path)
        problem = (
            _subject_problem(statement, att, capsule_hashes)
            or _content_problem(statement, names, read, covered_copies)
            or _run_id_problem(statement, subject_run_ids)
        )
        if problem:
            report.bad_subjects.append(f"{path} ({problem})")
        elif isinstance(statement, dict) and statement.get("predicateType") == PREDICATE_RUN:
            run_id = _predicate_run_id(statement)
            signed_runs.append(
                _SignedRun(run_id, frozenset(_statement_digests(statement)), covered_copies[-1])
            )
            if run_id and run_id not in report.signed_run_ids:
                report.signed_run_ids.append(run_id)
    report.bad_subjects.extend(_unsigned_claims(manifest, signed_runs))
    uncovered = sorted(
        n
        for n in names
        if n.startswith(_CAPSULE_COPY)
        and not n.endswith("/")
        and not any(n.startswith(prefix) for prefix in covered_copies)
    )
    if uncovered:
        report.bad_subjects.append(
            f"{len(uncovered)} file(s) under {_CAPSULE_COPY} are not in a capsule copy that a "
            f"verified run attestation signs (first: {uncovered[0]})"
        )
    return report


_CAPSULE_COPY = "run-capsule/"


@dataclass(frozen=True)
class _SignedRun:
    """A run statement whose signature, subject and content all verified."""

    run_id: str | None
    digests: frozenset[str]
    copy_prefix: str


def _subject_label(entry: dict[str, Any]) -> str:
    label = entry.get("run_id") or entry.get("capsule_hash") or "?"
    return f"{label!s:.80}"


def _subject_claim_problem(entry: object, signed_runs: list[_SignedRun]) -> str:
    """Why one manifest ``subject`` entry is not a run a verified statement signs."""
    if not isinstance(entry, dict) or not isinstance(entry.get("capsule_hash"), str):
        return f"manifest subject entry has no capsule_hash: {entry!r:.80}"
    label = _subject_label(entry)
    digest = _bare(entry["capsule_hash"])
    run = next((r for r in signed_runs if digest in r.digests), None)
    if run is None:
        return (
            f"manifest subject {label} is not covered by a verified signed run "
            "statement (manifest.json is unsigned; the run is not in the bundle's signatures)"
        )
    if "run_id" in entry and entry["run_id"] != run.run_id:
        return f"manifest subject {label}: run_id ≠ the run_id the run statement signs"
    path = entry.get("capsule_path_in_bundle")
    if path is not None and path != run.copy_prefix:
        return (
            f"manifest subject {label}: capsule_path_in_bundle {path!r:.80} ≠ the capsule "
            f"copy its signed run statement covers ({run.copy_prefix})"
        )
    return ""


def _unsigned_claims(manifest: dict[str, Any], signed_runs: list[_SignedRun]) -> list[str]:
    """Runs ``manifest.json`` claims that no verified run statement signs.

    The manifest is unsigned, so the set of runs a bundle reports must be exactly
    the set its verified run statements sign. Every ``subject`` entry must match
    one (hash, ``run_id``, ``capsule_path_in_bundle``), no entry may repeat, and a
    legacy top-level ``run_id`` must be one of the signed run ids. (A signed run
    the manifest omits already fails in :func:`_subject_problem`.)
    """
    raw = manifest.get("subject")
    entries = raw if isinstance(raw, list) else [raw] if raw is not None else []
    problems = [p for p in (_subject_claim_problem(e, signed_runs) for e in entries) if p]
    seen: set[str] = set()
    for entry in entries:
        if isinstance(entry, dict) and isinstance(entry.get("capsule_hash"), str):
            digest = _bare(entry["capsule_hash"])
            if digest in seen:
                label = _subject_label(entry)
                problems.append(f"manifest subject {label} is listed more than once")
            seen.add(digest)
    if not entries and signed_runs:
        problems.append("manifest.json has no subject for the signed run statements")
    if "run_id" in manifest:
        top = manifest["run_id"]
        if not any(top == r.run_id for r in signed_runs):
            problems.append(
                f"manifest run_id {top!r:.80} is not a run any verified run statement signs"
            )
    return problems


def _predicate_run_id(statement: dict[str, Any]) -> str | None:
    predicate = statement.get("predicate")
    run_id = predicate.get("run_id") if isinstance(predicate, dict) else None
    if isinstance(run_id, str) and run_id and "/" not in run_id and run_id not in (".", ".."):
        return run_id
    return None


def _statement_digests(statement: dict[str, Any]) -> set[str]:
    return {
        _bare(entry["digest"]["sha256"])
        for entry in _as_list(statement.get("subject"))
        if isinstance(entry, dict)
        and isinstance(entry.get("digest"), dict)
        and isinstance(entry["digest"].get("sha256"), str)
    }


def _content_problem(
    statement: object,
    names: set[str],
    read: Callable[[str], bytes],
    covered_copies: list[str],
) -> str:
    """Why a signed subject does not match the bundle's own files; "" when it does.

    A run statement's subject must be the capsule root of its ``run-capsule/`` copy
    (``run-capsule/<run_id>/`` in a capsule-set bundle); on success that copy's prefix
    is appended to *covered_copies*. A lineage statement's subject must be the
    SHA-256 of its ``edges.jsonl``.
    """
    if not isinstance(statement, dict):
        return ""
    predicate_type = statement.get("predicateType")
    digests = _statement_digests(statement)
    run_id = _predicate_run_id(statement)
    try:
        if predicate_type == PREDICATE_RUN:
            prefix = _CAPSULE_COPY
            if run_id and any(n.startswith(f"{_CAPSULE_COPY}{run_id}/") for n in names):
                prefix = f"{_CAPSULE_COPY}{run_id}/"
            members = [
                (n[len(prefix):], read(n))
                for n in names
                if n.startswith(prefix) and not n.endswith("/")
            ]
            if not members:
                return f"the bundle has no capsule copy under {prefix}"
            roots = {_bare(root_over_files(members, version=v)) for v in ROOT_VERSIONS}
            if not digests & roots:
                return f"the capsule copy under {prefix} does not hash to the signed subject"
            covered_copies.append(prefix)
        elif predicate_type == PREDICATE_LINEAGE:
            candidates = [f"lineage-subgraph/{run_id}/edges.jsonl"] if run_id else []
            candidates.append("lineage-subgraph/edges.jsonl")
            present = [c for c in candidates if c in names]
            if len(present) != 1:
                return f"expected one lineage edges.jsonl for this run, found {len(present)}"
            if hashlib.sha256(read(present[0])).hexdigest() not in digests:
                return f"{present[0]} does not hash to the signed subject"
    except (KeyError, ValueError, OSError) as exc:
        return f"cannot read the signed content ({type(exc).__name__}: {exc})"
    return ""


def _subject_problem(statement: object, att: dict[str, Any], capsule_hashes: set[str]) -> str:
    """Why a verified statement's subject does not match the manifest; "" when it does."""
    if not isinstance(statement, dict):
        return "statement is not a JSON object"
    label = att.get("predicate_type")
    if label and statement.get("predicateType") != label:
        return "statement predicateType ≠ manifest entry"
    digests = set()
    for entry in _as_list(statement.get("subject")):
        digest = entry.get("digest") if isinstance(entry, dict) else None
        if not isinstance(digest, dict) or not isinstance(digest.get("sha256"), str):
            return "statement subject has no sha256 digest"
        digests.add(_bare(digest["sha256"]))
    expected = {_bare(h) for h in _as_list(att.get("subject_hashes")) if isinstance(h, str)}
    if not digests or digests != expected:
        return "statement subject ≠ manifest subject_hashes"
    if statement.get("predicateType") == PREDICATE_RUN and not digests <= capsule_hashes:
        return "run subject ≠ manifest subject.capsule_hash"
    return ""


# ── the whole recipe over one archive ──────────────────────────────────────────

#: ``signer_trust`` values. ``anchored``: every attestation verified under a key the
#: caller trusts (``--public-key`` or an operator trust list). ``unanchored``: no trust
#: anchor was given, so the signatures verify under the key the bundle itself ships —
#: proof of consistency, not of origin. ``untrusted``: an anchor was given and a signer
#: is not in it. ``none``: no attestation signature verified at all.
SignerTrust = Literal["anchored", "unanchored", "untrusted", "none"]


class BundleArchiveError(ValueError):
    """The archive is not a NovaFabric Evidence Bundle that can be verified."""


class BundleTooLargeError(BundleArchiveError):
    """The archive declares more uncompressed bytes or members than the caller allows."""


@dataclass
class BundleVerification:
    """Outcome of the whole verification recipe over one Evidence Bundle archive.

    ``nova verify <bundle>.zip`` and ``POST /api/evidence/{bundle_id}/verify`` both
    build this with :func:`verify_bundle_archive`, so the two cannot disagree.
    """

    manifest: dict[str, Any]
    artifact_count: int
    modified: list[str]
    missing: list[str]
    unlisted: list[str]
    signatures: BundleSignatureReport
    anchored: bool
    has_timestamp_token: bool
    duplicate_members: list[str] = field(default_factory=list)

    @property
    def digests_ok(self) -> bool:
        return not self.modified

    @property
    def complete(self) -> bool:
        return not self.missing

    @property
    def no_extras(self) -> bool:
        return not self.unlisted and not self.duplicate_members

    @property
    def signature_ok(self) -> bool:
        """Every listed attestation is signed and its DSSE signature verifies."""
        s = self.signatures
        return s.attestations_checked > 0 and not s.bad_signatures and not s.unsigned

    @property
    def ok(self) -> bool:
        """Every recipe step passed (and, when anchored, every signer is trusted)."""
        return self.digests_ok and self.complete and self.no_extras and self.signatures.ok

    @property
    def signer_trust(self) -> SignerTrust:
        if self.signatures.attestations_checked == 0:
            return "none"
        if not self.anchored:
            return "unanchored"
        return "untrusted" if self.signatures.signer_not_trusted else "anchored"

    def problems(self) -> list[str]:
        """One line per failed check, in recipe order — for an API ``errors`` list."""
        s = self.signatures
        out = [f"artifact modified: {p}" for p in self.modified]
        out += [f"artifact missing: {p}" for p in self.missing]
        out += [f"file not in manifest: {p}" for p in self.unlisted]
        out += [f"duplicate archive member: {p}" for p in self.duplicate_members]
        if not s.manifest_hash_ok:
            out.append("manifest_hash does not match the manifest")
        out += [f"bad signature: {p}" for p in s.bad_signatures]
        out += [f"unsigned attestation: {p}" for p in s.unsigned]
        out += [f"subject: {p}" for p in s.bad_subjects]
        out += [f"signer is not a trusted key: {p}" for p in s.signer_not_trusted]
        out += list(s.errors)
        return out


def _member_digest(zf: zipfile.ZipFile, name: str) -> str:
    digest = hashlib.sha256()
    with zf.open(name) as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _check_declared_size(
    zf: zipfile.ZipFile, max_uncompressed_bytes: int | None, max_members: int | None
) -> None:
    infos = zf.infolist()
    if max_members is not None and len(infos) > max_members:
        raise BundleTooLargeError(f"the archive has {len(infos)} members (limit {max_members})")
    if max_uncompressed_bytes is not None:
        total = sum(i.file_size for i in infos)
        if total > max_uncompressed_bytes:
            raise BundleTooLargeError(
                f"the archive declares {total} uncompressed bytes "
                f"(limit {max_uncompressed_bytes})"
            )


#: Upper bound on ``manifest.json``, declared and inflated. A ~300 KB archive can
#: inflate to hundreds of MB of whitespace; nothing reads the manifest whole.
MAX_MANIFEST_BYTES = 64 * 1024 * 1024


def read_bundle_manifest(zf: zipfile.ZipFile) -> dict[str, Any]:
    """``manifest.json`` of an open bundle, parsed, never read past :data:`MAX_MANIFEST_BYTES`.

    The one way the verifier and ``nova serve``'s list/detail routes read it.

    Raises:
        BundleArchiveError: no ``manifest.json``, or it is not a JSON object.
        BundleTooLargeError: it declares, or inflates to, more than the bound.
    """
    limit = MAX_MANIFEST_BYTES
    try:
        info = zf.getinfo("manifest.json")
    except KeyError:
        raise BundleArchiveError(
            "the archive has no manifest.json — not an Evidence Bundle"
        ) from None
    if info.file_size > limit:
        raise BundleTooLargeError(
            f"manifest.json declares {info.file_size} bytes (limit {limit})"
        )
    with zf.open(info) as handle:
        raw = handle.read(limit + 1)
    if len(raw) > limit:
        raise BundleTooLargeError(f"manifest.json is larger than {limit} bytes")
    try:
        manifest = json.loads(raw)
    except ValueError as exc:
        raise BundleArchiveError(f"manifest.json is not valid JSON: {exc}") from exc
    if not isinstance(manifest, dict):
        raise BundleArchiveError("manifest.json is not a JSON object")
    return manifest


def _duplicates(listed: list[str]) -> list[str]:
    seen: set[str] = set()
    dupes: set[str] = set()
    for name in listed:
        (dupes if name in seen else seen).add(name)
    return sorted(dupes)


def verify_bundle_archive(
    zf: zipfile.ZipFile,
    *,
    trusted_public_pems: Sequence[bytes] = (),
    max_uncompressed_bytes: int | None = None,
    max_members: int | None = None,
) -> BundleVerification:
    """Run the bundle's whole verification recipe over an open archive.

    Recomputes every listed artifact digest, flags missing, unlisted and duplicate
    members, and runs :func:`verify_bundle_signatures` (``manifest_hash``, DSSE
    signatures, subjects recomputed from the bundle's own content). With
    *trusted_public_pems* every signer must be one of those keys; without them the
    result's ``signer_trust`` is ``unanchored``.

    A duplicate member name fails: ``zipfile`` reads the last entry of that name and
    other unzip tools may extract the first, so the bytes verified need not be the
    bytes a recipient sees.

    Raises:
        BundleArchiveError: no ``manifest.json``, or it is not a JSON object.
        BundleTooLargeError: the archive exceeds *max_uncompressed_bytes* (sum of
            declared member sizes) or *max_members*, or ``manifest.json`` exceeds
            :data:`MAX_MANIFEST_BYTES`.
    """
    _check_declared_size(zf, max_uncompressed_bytes, max_members)
    listed = zf.namelist()
    names = set(listed)
    manifest = read_bundle_manifest(zf)

    raw_artifacts = _as_list(manifest.get("artifacts"))
    artifacts = [a for a in raw_artifacts if isinstance(a, dict)]
    modified: list[str] = []
    missing: list[str] = []
    for art in artifacts:
        rel = str(art.get("path", ""))
        if rel not in names:
            missing.append(rel)
        elif _member_digest(zf, rel) != art.get("sha256", ""):
            modified.append(rel)
    # An addition is a modification too: a file the manifest never accounted for fails.
    unlisted = sorted(names - {str(a.get("path", "")) for a in artifacts} - {"manifest.json"})
    signatures = verify_bundle_signatures(
        manifest, zf.read, names, trusted_public_pems=trusted_public_pems
    )
    has_token = "manifest.dsse.tsr" in names and zf.getinfo("manifest.dsse.tsr").file_size > 0
    return BundleVerification(
        manifest=manifest,
        artifact_count=len(raw_artifacts),
        modified=modified,
        missing=missing,
        unlisted=unlisted,
        signatures=signatures,
        anchored=bool(trusted_public_pems),
        has_timestamp_token=has_token,
        duplicate_members=_duplicates(listed),
    )


# ── operator trust list ────────────────────────────────────────────────────────

_PEM_BLOCK = re.compile(rb"-----BEGIN PUBLIC KEY-----\r?\n.+?-----END PUBLIC KEY-----", re.DOTALL)
#: Bounds on the operator trust list: it is configuration, not data.
MAX_TRUSTED_KEYS_BYTES = 1 << 20
MAX_TRUSTED_KEYS = 256
_OPEN_FLAGS = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)


class TrustedKeysError(Exception):
    """The configured evidence-signer trust list is missing, unreadable or malformed.

    A configured anchor that cannot be used is an error, never a silent downgrade to
    "no anchor": that would turn an operator's pinning into ``unanchored``.
    """


def _read_trust_list(path: Path) -> bytes:
    try:
        fd = os.open(path, _OPEN_FLAGS)
    except OSError as exc:
        raise TrustedKeysError(f"cannot open the evidence trust list {path}: {exc}") from exc
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise TrustedKeysError(f"the evidence trust list {path} is not a regular file")
        os.set_blocking(fd, True)
        data = handle.read(MAX_TRUSTED_KEYS_BYTES + 1)
    if len(data) > MAX_TRUSTED_KEYS_BYTES:
        raise TrustedKeysError(
            f"the evidence trust list {path} is larger than {MAX_TRUSTED_KEYS_BYTES} bytes"
        )
    return data


def load_trusted_public_keys(path: Path) -> list[bytes]:
    """Parse a PEM file holding one or more Ed25519 ``PUBLIC KEY`` blocks.

    The format is what ``nova verify --public-key`` takes, concatenated. The file is
    opened non-blocking and must be a regular file, so a FIFO never hangs the caller,
    and at most :data:`MAX_TRUSTED_KEYS_BYTES` are read. A link is followed: the list
    is operator configuration, often mounted through one (Kubernetes secrets).

    Raises:
        TrustedKeysError: missing, not a regular file, too large, holding no key, too
            many keys, or a block that is not an Ed25519 public key.
    """
    blocks: list[bytes] = _PEM_BLOCK.findall(_read_trust_list(path))
    if not blocks:
        raise TrustedKeysError(f"the evidence trust list {path} holds no PUBLIC KEY block")
    if len(blocks) > MAX_TRUSTED_KEYS:
        raise TrustedKeysError(
            f"the evidence trust list {path} holds {len(blocks)} keys (limit {MAX_TRUSTED_KEYS})"
        )
    for i, block in enumerate(blocks, 1):
        try:
            key = serialization.load_pem_public_key(block)
        except (ValueError, UnsupportedAlgorithm) as exc:
            raise TrustedKeysError(f"{path}: key {i} is not a valid public key ({exc})") from exc
        if not isinstance(key, Ed25519PublicKey):
            raise TrustedKeysError(
                f"{path}: key {i} is not an Ed25519 key; Evidence Bundles are signed with Ed25519"
            )
    return blocks
