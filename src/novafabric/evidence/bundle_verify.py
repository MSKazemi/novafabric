"""Signature, manifest-hash and subject checks for an Evidence Bundle.

Every bundle carries a verification recipe (``VERIFIER_INSTRUCTIONS`` in
:mod:`novafabric.evidence.bundle`) with four steps. ``nova verify <bundle>.zip``
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
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization

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


def _as_list(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


def verify_bundle_signatures(
    manifest: dict[str, Any],
    read: Callable[[str], bytes],
    names: set[str],
    trusted_public_pem: bytes | None = None,
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
    trusted_fp = _public_key_fingerprint(trusted_public_pem) if trusted_public_pem else None
    capsule_hashes = _capsule_hashes(manifest)
    covered_copies: list[str] = []

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
        if trusted_fp is not None and fingerprint != trusted_fp:
            report.signer_not_trusted.append(path)
        problem = _subject_problem(statement, att, capsule_hashes) or _content_problem(
            statement, names, read, covered_copies
        )
        if problem:
            report.bad_subjects.append(f"{path} ({problem})")
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
