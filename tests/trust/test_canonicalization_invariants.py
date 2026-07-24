"""Safety-net regression tests for JSON canonicalization used by signing/hashing.

Enterprise-hardening Phase 0.1. NovaFabric currently canonicalizes signed and
hashed payloads two different ways:

* ``jcs.canonicalize`` (RFC 8785) — used by the ``promote`` proposal/approval
  digest path (``promote/verifier.py``, ``promote/predicates.py``).
* ``json.dumps(..., sort_keys=True, separators=(",", ":"))`` — used by the
  HMAC event-signing path (``events/signing.py``) and ~65 other sites.

These two encodings are **not byte-identical** for realistic payloads (non-ASCII
strings, floats). That is safe *today* only because each sign/verify pair uses a
single encoding on both sides. This test:

1. Pins each existing sign/verify pair as internally consistent (round-trip).
2. Documents — as an executable assertion — that the two encodings diverge, so a
   future consolidation (Phase 2) cannot silently change signed bytes without a
   test failure here forcing a compatibility shim.

If Phase 2 unifies canonicalization, the divergence assertion is expected to be
updated deliberately (with a migration/compat shim), never deleted silently.
"""

from __future__ import annotations

import hashlib
import json

import jcs  # type: ignore[import-untyped]

from novafabric.events.signing import canonical_body, sign_record, verify_record


def _sort_keys_default(obj: dict) -> bytes:
    """The encoding the NovaSeal/in-toto signing paths actually use.

    ``trust/novaseal/merkle.py:375/628``, ``object_capsule_store/novaseal_client.py:83``
    and ``trust/ledger/_verify.py:181`` all call ``json.dumps(..., sort_keys=True,
    separators=(",", ":"))`` with the *default* ``ensure_ascii=True``.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sort_keys_utf8(obj: dict) -> bytes:
    """The variant used by ``events/signing.py`` (ensure_ascii=False)."""
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


# ---------------------------------------------------------------------------
# 1. Divergence is real — the core P0 hazard, pinned as an assertion.
# ---------------------------------------------------------------------------


def test_signing_default_ascii_escape_diverges_from_jcs_on_non_ascii() -> None:
    """The real signing paths ASCII-escape (``\\uXXXX``); jcs emits raw UTF-8.

    A NovaSeal-signed log entry or in-toto statement containing any non-ASCII
    character (accented filename, non-Latin prompt) therefore produces different
    bytes than an RFC 8785 verifier would compute. This is the concrete P0.
    """
    payload = {"z": "café", "a": "naïve", "n": 1}
    assert jcs.canonicalize(payload) != _sort_keys_default(payload)


def test_jcs_and_sort_keys_diverge_on_float_formatting() -> None:
    """RFC 8785 uses ECMAScript number formatting; json.dumps does not."""
    payload = {"ratio": 1.0, "big": 1e21, "small": 0.0000001}
    assert jcs.canonicalize(payload) != _sort_keys_default(payload)


def test_ensure_ascii_false_matches_jcs_on_non_ascii_strings() -> None:
    """Bounds the hazard: with ensure_ascii=False the string encoding coincides
    with jcs for the plain-string case. So ``events/signing.py`` (which uses
    ensure_ascii=False) is closer to RFC 8785 than the NovaSeal paths — the
    consolidation target should be the ensure_ascii=False / jcs form.
    """
    payload = {"z": "café", "a": "naïve", "n": 1}
    assert jcs.canonicalize(payload) == _sort_keys_utf8(payload)


def test_jcs_and_sort_keys_agree_on_plain_ascii_ints() -> None:
    """For the trivial ASCII/integer case all encodings coincide.

    This bounds the hazard: divergence is specifically non-ASCII escaping and
    non-integer numbers — exactly what a consolidation must preserve.
    """
    payload = {"b": 2, "a": 1, "c": "ok"}
    assert jcs.canonicalize(payload) == _sort_keys_default(payload) == _sort_keys_utf8(payload)


# ---------------------------------------------------------------------------
# 2. Each sign/verify pair is internally consistent (round-trip).
# ---------------------------------------------------------------------------


def test_event_signing_roundtrip_ascii() -> None:
    secret = b"unit-test-secret"
    record = {"event": "capture.start", "run_id": "r-1", "ts": 1700000000}
    signed = sign_record(record, secret, keyid="k1")
    assert verify_record(signed, secret) is True


def test_event_signing_roundtrip_non_ascii() -> None:
    """Round-trip must hold even for the payloads where encodings diverge."""
    secret = b"unit-test-secret"
    record = {"event": "capture.start", "note": "café ☕", "n": 3}
    signed = sign_record(record, secret, keyid="k1")
    assert verify_record(signed, secret) is True


def test_event_signing_rejects_tampered_body() -> None:
    secret = b"unit-test-secret"
    record = {"event": "capture.start", "run_id": "r-1"}
    signed = sign_record(record, secret, keyid="k1")
    signed["run_id"] = "r-2"  # tamper after signing
    assert verify_record(signed, secret) is False


def test_event_signing_wrong_secret_fails() -> None:
    record = {"event": "capture.start", "run_id": "r-1"}
    signed = sign_record(record, b"secret-a", keyid="k1")
    assert verify_record(signed, b"secret-b") is False


def test_canonical_body_excludes_signature_field() -> None:
    """The signature field must not be part of its own signing input."""
    record = {"a": 1, "signature": {"alg": "hmac-sha256", "value": "deadbeef"}}
    assert canonical_body(record) == canonical_body({"a": 1})


# ---------------------------------------------------------------------------
# 3. promote digest path (jcs) is stable and reproducible.
# ---------------------------------------------------------------------------


def test_promote_digest_is_jcs_stable() -> None:
    """The proposal digest must be reproducible from the same object regardless
    of key insertion order — the property ``promote/verifier.py`` relies on."""
    envelope_a = {"kind": "proposal", "run_id": "r-1", "score": 0.9}
    envelope_b = {"score": 0.9, "run_id": "r-1", "kind": "proposal"}
    digest_a = hashlib.sha256(jcs.canonicalize(envelope_a)).hexdigest()
    digest_b = hashlib.sha256(jcs.canonicalize(envelope_b)).hexdigest()
    assert digest_a == digest_b
