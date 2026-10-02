# Copyright 2024 NovaFabric Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""In-toto binding for consent receipts and withdrawals (ADR-0150 P3 remainder).

A signed in-toto Statement v1, wrapped in a DSSE envelope by the one shared
writer in :mod:`novafabric.evidence.intoto`, that commits to the consent state
of a capsule at attestation time:

* ``subject`` — one entry per receipt, ``consent/<consent_id>`` with the
  receipt's own ``receipt_digest`` (stable across a later withdrawal, because
  the receipt digest excludes ``withdrawn_at``);
* ``predicate`` — per receipt: ``consent_id``, ``receipt_digest``,
  ``withdrawn_at`` and a ``withdrawal_digest`` that binds the withdrawal event
  (domain-tagged SHA-256 over ``consent_id`` + ``receipt_digest`` +
  ``withdrawn_at``), plus ``consent_set_digest`` over the whole list and the
  capsule's RFC 6962 Merkle root at that moment.

Merkle constructions: the capsule root is computed with
:func:`novafabric.evidence.merkle.capsule_merkle_root` — the RFC 6962
construction the Evidence Bundle uses. The NovaSeal pairwise+pad construction
(``trust/novaseal/merkle.py``) is **not** used or mixed here: the two roots are
incompatible, and this attestation makes no claim about a NovaSeal seal root.

Nothing is written into the capsule: a withdrawal already rewrites
``capsule.yaml``, and a sealed capsule is immutable. The envelope is a sidecar
file the caller stores next to (or in) an Evidence Bundle.

Verification (offline, fail-closed) checks the DSSE signature against a
supplied public key (and that the envelope ``keyid`` names that key), the
statement/predicate types, that the subjects equal the predicate's receipts,
that the predicate's own digests are self-consistent, and compares every
attested receipt with the capsule. Verdicts:

``ok``       attestation signed, intact, and still describes the capsule;
``stale``    nothing was tampered with, but the capsule moved on — a consent
             was withdrawn or recorded after the attestation (re-attest);
``invalid``  signature, structure, or content failed — including a receipt
             edited since attestation, a withdrawal removed or moved, or a
             consent dropped from the capsule.

**Record-only (I-4).** This proves what the capsule recorded and when it was
attested; it does not assert that any consent was legally valid.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from novafabric.evidence.intoto import (
    DSSE_PAYLOAD_TYPE,
    INTOTO_STATEMENT_TYPE,
    dsse_sign,
    dsse_verify,
    make_intoto_statement,
)
from novafabric.evidence.merkle import capsule_merkle_root
from novafabric.evidence.signing import verify_with_pem
from novafabric.hitl.consent import (
    CONSENT_NOTICE,
    ConsentReceipt,
    load_consents,
    receipt_digest_matches,
)

CONSENT_ATTESTATION_PREDICATE_TYPE = "https://novafabric.io/consent-attestation/v0"
SCHEMA_VERSION = "0.1.0"

#: Domain tags keep these digests from colliding with any other SHA-256 of JSON.
WITHDRAWAL_DOMAIN = "novafabric.hitl.consent-withdrawal.v1"
CONSENT_SET_DOMAIN = "novafabric.hitl.consent-set.v1"

Status = Literal["ok", "stale", "invalid"]


class ConsentAttestationError(Exception):
    """A consent attestation cannot be built (nothing to attest, or a defective receipt)."""


def _sha256_json(domain: str, body: Any) -> str:
    canonical = json.dumps(
        {"domain": domain, "body": body}, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return "sha256:" + hashlib.sha256(canonical.encode("ascii")).hexdigest()


def withdrawal_digest(consent_id: str, receipt_digest: str, withdrawn_at: str) -> str:
    """Digest binding one withdrawal event to the receipt it withdrew."""
    return _sha256_json(
        WITHDRAWAL_DOMAIN,
        {"consent_id": consent_id, "receipt_digest": receipt_digest, "withdrawn_at": withdrawn_at},
    )


def consent_set_digest(entries: list[dict[str, Any]]) -> str:
    """Digest over the attested entries, order-independent (sorted by ``consent_id``)."""
    return _sha256_json(CONSENT_SET_DOMAIN, sorted(entries, key=lambda e: str(e.get("consent_id"))))


def _entry(receipt: ConsentReceipt) -> dict[str, Any]:
    out: dict[str, Any] = {
        "consent_id": receipt.consent_id,
        "receipt_digest": receipt.receipt_digest,
        "withdrawn_at": receipt.withdrawn_at,
        "withdrawal_digest": (
            withdrawal_digest(receipt.consent_id, receipt.receipt_digest, receipt.withdrawn_at)
            if receipt.withdrawn_at is not None
            else None
        ),
    }
    return out


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_consent_statement(
    capsule: Mapping[str, Any],
    *,
    capsule_dir: Path | None = None,
    attested_at: str | None = None,
) -> dict[str, Any]:
    """Build the (unsigned) in-toto statement for a capsule's consent receipts.

    Raises:
        ConsentAttestationError: no receipts are stored, ``run_id`` is missing,
            or any stored receipt is malformed or fails its digest check —
            a tampered receipt is never attested.
    """
    run_id = capsule.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise ConsentAttestationError("capsule has no run_id")
    loaded = load_consents(capsule)
    if loaded.defects:
        raise ConsentAttestationError(
            f"{len(loaded.defects)} stored consent receipt(s) are malformed; refusing to attest"
        )
    if not loaded.records:
        raise ConsentAttestationError("capsule records no consent receipts; nothing to attest")
    for _, rec in loaded.records:
        if not receipt_digest_matches(rec):
            raise ConsentAttestationError(
                f"consent receipt {rec.consent_id!r} fails its digest check; refusing to attest"
            )
    ids = [rec.consent_id for _, rec in loaded.records]
    if len(set(ids)) != len(ids):
        raise ConsentAttestationError("a consent_id is recorded more than once; refusing to attest")
    entries = [_entry(rec) for _, rec in loaded.records]
    predicate: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "attested_at": attested_at or _now(),
        "consents": entries,
        "consent_set_digest": consent_set_digest(entries),
        "notice": CONSENT_NOTICE,
    }
    if capsule_dir is not None:
        predicate["capsule_merkle_root"] = capsule_merkle_root(capsule_dir)
    statement = make_intoto_statement(
        predicate_type=CONSENT_ATTESTATION_PREDICATE_TYPE,
        subject_name=f"consent/{entries[0]['consent_id']}",
        subject_sha256=entries[0]["receipt_digest"],
        predicate=predicate,
    )
    statement["subject"] = [
        {
            "name": f"consent/{e['consent_id']}",
            "digest": {"sha256": str(e["receipt_digest"]).removeprefix("sha256:")},
        }
        for e in entries
    ]
    return statement


def attest_consents(
    capsule: Mapping[str, Any],
    signer: Any,
    *,
    capsule_dir: Path | None = None,
    attested_at: str | None = None,
) -> dict[str, Any]:
    """Build the consent statement and DSSE-sign it (the shared DSSE writer)."""
    statement = build_consent_statement(capsule, capsule_dir=capsule_dir, attested_at=attested_at)
    envelope: dict[str, Any] = dsse_sign(statement, signer)
    return envelope


@dataclass(frozen=True)
class AttestationCheck:
    """One named verification check."""

    name: str
    ok: bool
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail}


@dataclass
class AttestationVerification:
    """Result of :func:`verify_consent_attestation`."""

    status: Status = "ok"
    checks: list[AttestationCheck] = field(default_factory=list)
    stale_reasons: list[str] = field(default_factory=list)
    run_id: str | None = None
    capsule_root_matches: bool | None = None

    def fail(self, name: str, detail: str) -> None:
        self.checks.append(AttestationCheck(name, False, detail))
        self.status = "invalid"

    def passed(self, name: str, detail: str = "") -> None:
        self.checks.append(AttestationCheck(name, True, detail))

    def stale(self, reason: str) -> None:
        self.stale_reasons.append(reason)
        if self.status == "ok":
            self.status = "stale"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "run_id": self.run_id,
            "checks": [c.to_dict() for c in self.checks],
            "stale_reasons": list(self.stale_reasons),
            "capsule_root_matches": self.capsule_root_matches,
        }


def _keyid_for(public_pem: bytes) -> str:
    return "sha256:" + hashlib.sha256(public_pem).hexdigest()


def _verify_envelope(
    envelope: Mapping[str, Any], public_pem: bytes, result: AttestationVerification
) -> dict[str, Any] | None:
    """Signature + framing checks; the decoded statement, or ``None`` after a failure."""
    try:
        sigs = envelope["signatures"]
        if envelope["payloadType"] != DSSE_PAYLOAD_TYPE:
            result.fail("envelope", f"unexpected payloadType {envelope['payloadType']!r}")
            return None
        if len(sigs) != 1 or sigs[0].get("keyid") != _keyid_for(public_pem):
            result.fail("signature", "envelope keyid does not name the supplied public key")
            return None
        base64.b64decode(envelope["payload"], validate=True)
        statement = dsse_verify(
            dict(envelope), lambda pae, sig: verify_with_pem(public_pem, pae, sig)
        )
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        result.fail("signature", f"DSSE envelope rejected ({type(exc).__name__}: {exc})")
        return None
    result.passed("signature", "DSSE signature verifies against the supplied key")
    return statement


def verify_consent_attestation(
    envelope: Mapping[str, Any],
    capsule: Mapping[str, Any],
    *,
    public_pem: bytes,
    capsule_dir: Path | None = None,
) -> AttestationVerification:
    """Verify a consent attestation offline against the capsule (fail-closed)."""
    result = AttestationVerification()
    statement = _verify_envelope(envelope, public_pem, result)
    if statement is None:
        return result
    predicate = statement.get("predicate")
    if (
        statement.get("_type") != INTOTO_STATEMENT_TYPE
        or statement.get("predicateType") != CONSENT_ATTESTATION_PREDICATE_TYPE
        or not isinstance(predicate, dict)
    ):
        result.fail("statement", "not an in-toto v1 consent-attestation statement")
        return result
    result.passed("statement")
    result.run_id = predicate.get("run_id") if isinstance(predicate.get("run_id"), str) else None

    attested = predicate.get("consents")
    if not isinstance(attested, list) or not all(isinstance(e, dict) for e in attested):
        result.fail("predicate", "consents is not a list of entries")
        return result

    # Predicate self-consistency: set digest and every withdrawal digest.
    if predicate.get("consent_set_digest") != consent_set_digest(attested):
        result.fail("consent_set_digest", "predicate consents do not match consent_set_digest")
    else:
        result.passed("consent_set_digest")
    wd_ok = True
    for e in attested:
        w = e.get("withdrawn_at")
        expect = (
            withdrawal_digest(str(e.get("consent_id")), str(e.get("receipt_digest")), w)
            if isinstance(w, str)
            else None
        )
        if e.get("withdrawal_digest") != expect:
            wd_ok = False
            result.fail("withdrawal_digest", f"{e.get('consent_id')}: withdrawal digest mismatch")
    if wd_ok:
        result.passed("withdrawal_digest")

    # Subjects must be exactly the attested receipts.
    want = {
        (f"consent/{e.get('consent_id')}", str(e.get("receipt_digest")).removeprefix("sha256:"))
        for e in attested
    }
    have = {
        (s.get("name"), (s.get("digest") or {}).get("sha256"))
        for s in statement.get("subject", [])
        if isinstance(s, dict)
    }
    if want != have or len(statement.get("subject", [])) != len(attested):
        result.fail("subjects", "statement subjects do not equal the attested receipts")
    else:
        result.passed("subjects")

    if predicate.get("run_id") != capsule.get("run_id"):
        result.fail("run_id", "attestation run_id differs from the capsule's")
        return result
    result.passed("run_id")

    _compare_with_capsule(attested, capsule, result)

    if capsule_dir is not None and isinstance(predicate.get("capsule_merkle_root"), str):
        result.capsule_root_matches = predicate["capsule_merkle_root"] == capsule_merkle_root(
            capsule_dir
        )
        if not result.capsule_root_matches:
            # Informational: any later annotation (incl. a withdrawal) moves the root.
            result.stale("capsule Merkle root changed since attestation")
    return result


def _compare_with_capsule(
    attested: list[dict[str, Any]], capsule: Mapping[str, Any], result: AttestationVerification
) -> None:
    loaded = load_consents(capsule)
    if loaded.defects:
        result.fail("capsule", f"{len(loaded.defects)} stored consent receipt(s) are malformed")
    current = {rec.consent_id: rec for _, rec in loaded.records}
    all_ok = not loaded.defects
    for e in attested:
        cid = str(e.get("consent_id"))
        rec = current.get(cid)
        if rec is None:
            result.fail("receipt", f"{cid}: attested receipt is missing from the capsule")
            all_ok = False
            continue
        if not receipt_digest_matches(rec) or rec.receipt_digest != e.get("receipt_digest"):
            result.fail("receipt", f"{cid}: receipt differs from what was attested")
            all_ok = False
            continue
        was, now = e.get("withdrawn_at"), rec.withdrawn_at
        if was is not None and now != was:
            result.fail("withdrawal", f"{cid}: attested withdrawal was removed or moved")
            all_ok = False
        elif was is None and now is not None:
            result.stale(f"{cid}: withdrawn after attestation (at {now})")
    attested_ids = {str(e.get("consent_id")) for e in attested}
    for cid in sorted(set(current) - attested_ids):
        result.stale(f"{cid}: recorded after attestation")
    if all_ok:
        result.passed("receipts", "every attested receipt matches the capsule")
