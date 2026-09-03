"""Every signature in a DSSE envelope must be considered, not just the first.

Until 2026-09-02 `verify_envelope` read `signatures[0]` and ignored the rest, so a
second signature was neither honoured nor detected. That was the recorded blocker
for ADR-0151 NF-191: a hybrid classic+post-quantum envelope carries two signatures,
and a verifier that understands only one algorithm must still be able to accept it.

The load-bearing guarantee here is the *equivalence* one — for a single-signature
envelope, which is everything this project produces today, behaviour and error
messages are unchanged. That is asserted, not assumed.
"""

from __future__ import annotations

import json

import pytest

from novafabric.trust.novaseal.envelope import (
    EnvelopeError,
    _b64_encode,
    create_envelope,
    verify_envelope,
)


@pytest.fixture(scope="module")
def ec_key_pair(tmp_path_factory):
    """A P-256 key pair and self-signed cert (same shape as test_envelope.py)."""
    import datetime

    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.hashes import SHA256
    from cryptography.x509.oid import NameOID

    tmp = tmp_path_factory.mktemp("multisig-keys")
    key = ec.generate_private_key(ec.SECP256R1())
    key_path = tmp / "test.key"
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )

    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "NovaSeal-Test")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.now(datetime.timezone.utc))
        .not_valid_after(
            datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=365)
        )
        .sign(key, SHA256())
    )
    cert_path = tmp / "test.crt"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return key_path, cert_path


@pytest.fixture()
def envelope(ec_key_pair) -> bytes:
    key_path, cert_path = ec_key_pair
    return create_envelope(b'{"run_id":"01RUNA"}', key_path, cert_path)


def _with_signatures(envelope_bytes: bytes, entries: list[dict]) -> bytes:
    env = json.loads(envelope_bytes)
    env["signatures"] = entries
    return json.dumps(env, separators=(",", ":")).encode()


def _corrupt(entry: dict) -> dict:
    """Same entry, with a signature that cannot verify."""
    bad = dict(entry)
    bad["sig"] = _b64_encode(b"\x00" * 64)
    return bad


# ── the equivalence guarantee ────────────────────────────────────────────────


def test_a_single_valid_signature_still_verifies(envelope: bytes) -> None:
    assert verify_envelope(envelope) is True


def test_a_single_invalid_signature_raises_the_same_message_as_before(
    envelope: bytes,
) -> None:
    """One signature must not gain a multi-signature error wrapper."""
    entry = json.loads(envelope)["signatures"][0]
    broken = _with_signatures(envelope, [_corrupt(entry)])

    with pytest.raises(EnvelopeError) as excinfo:
        verify_envelope(broken)

    message = str(excinfo.value)
    assert message == "DSSE signature verification failed: signature mismatch"
    assert "signatures[" not in message, "single-sig error must not be wrapped"


def test_both_policies_agree_on_a_single_signature(envelope: bytes) -> None:
    assert verify_envelope(envelope, require="any") is True
    assert verify_envelope(envelope, require="all") is True


# ── hybrid-OR, which is what NF-191 needs ────────────────────────────────────


def test_a_valid_second_signature_is_now_honoured(envelope: bytes) -> None:
    """The case the old code could not express: slot 0 unusable, slot 1 good.

    Before this change the envelope was rejected on `signatures[0]` alone.
    """
    good = json.loads(envelope)["signatures"][0]
    both = _with_signatures(envelope, [_corrupt(good), good])

    assert verify_envelope(both, require="any") is True


def test_a_valid_first_signature_still_wins_when_the_second_is_bad(
    envelope: bytes,
) -> None:
    good = json.loads(envelope)["signatures"][0]
    both = _with_signatures(envelope, [good, _corrupt(good)])

    assert verify_envelope(both, require="any") is True


def test_all_signatures_failing_names_every_one_of_them(envelope: bytes) -> None:
    """A hybrid failure must not report only the first reason."""
    good = json.loads(envelope)["signatures"][0]
    both = _with_signatures(envelope, [_corrupt(good), _corrupt(good)])

    with pytest.raises(EnvelopeError) as excinfo:
        verify_envelope(both, require="any")

    message = str(excinfo.value)
    assert "signatures[0]" in message
    assert "signatures[1]" in message
    assert "2 tried" in message


# ── threshold ────────────────────────────────────────────────────────────────


def test_require_all_rejects_an_envelope_with_one_bad_signature(
    envelope: bytes,
) -> None:
    """This is the case the old code silently accepted: a forged extra signature."""
    good = json.loads(envelope)["signatures"][0]
    both = _with_signatures(envelope, [good, _corrupt(good)])

    with pytest.raises(EnvelopeError) as excinfo:
        verify_envelope(both, require="all")

    assert "1 of 2" in str(excinfo.value)
    assert "signatures[1]" in str(excinfo.value)


def test_require_all_accepts_when_every_signature_verifies(envelope: bytes) -> None:
    good = json.loads(envelope)["signatures"][0]
    both = _with_signatures(envelope, [good, good])

    assert verify_envelope(both, require="all") is True


# ── policy argument ──────────────────────────────────────────────────────────


def test_an_unknown_policy_is_refused(envelope: bytes) -> None:
    with pytest.raises(EnvelopeError, match="Unknown verification policy"):
        verify_envelope(envelope, require="most")


def test_an_envelope_with_no_signatures_is_still_refused(envelope: bytes) -> None:
    with pytest.raises(EnvelopeError, match="no signatures"):
        verify_envelope(_with_signatures(envelope, []))
