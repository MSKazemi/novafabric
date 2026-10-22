"""The degraded ("structural check only") RFC 3161 path must not accept a forgery.

ADR-0297 §S3 keeps a fallback for tokens the strict DER parser rejects (BER encodings,
unusual structures): it is reported ``strict=False`` and "structural check only", never as
a plain OK. That fallback is an accepted decision and stays. What it must not do is
report ``timestamp_ok=True`` for a token nobody can show a TSA signed:

* **unverifiable signature** — ``_legacy_structural_check`` treated "the CMS signature
  could not be extracted" (``None``) like "it verified". A granted status plus a
  ContentInfo-shaped blob holding the envelope hash, with no signer at all, verified.
* **S3 revived** — the fallback found the imprint *anywhere* in the response and never
  checked the signed ``messageDigest`` against the ``TSTInfo``. Any BER-only byte (here a
  long-form length for a short PKIStatusInfo) sends a token off the strict path, so a
  genuine token for envelope A with its imprint swapped to B verified as a timestamp of B.

Every token below is real ASN.1, built by :mod:`tests.seal._tsa_pki` and signed by an
in-memory TSA; only the BER tweak and the forged bytes differ from a genuine token.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519
from typer.testing import CliRunner

from novafabric.cli.main import app
from novafabric.trust.novaseal import KeyConfig, NovaSeal, verify_seal_dir
from novafabric.trust.novaseal.timestamp import check_timestamp

from ._tsa_pki import (
    OID_RSA_PSS,
    TokenSpec,
    TsaNode,
    integer,
    make_tsa_cert,
    make_tsr,
    octets,
    oid,
    seq,
    tlv,
)
from ._x509_pki import Node, make_cert, make_pki

runner = CliRunner()
_OID_SIGNED_DATA = "1.2.840.113549.1.7.2"
ENVELOPE = b'{"payloadType":"application/vnd.novafabric.capsule+json","payload":"e30="}'


@pytest.fixture(scope="module")
def tsa() -> TsaNode:
    ca: Node = make_cert("degraded-tsa-root", issuer=None, ca=True)
    return make_tsa_cert(ca, cn="degraded-tsa")


def _ber(token: bytes) -> bytes:
    """Wrap a bare TimeStampToken in a TimeStampResp whose PKIStatusInfo length is BER.

    ``30 81 03`` is a valid BER length (long form) but not DER, so the strict parser
    refuses the response and :func:`check_timestamp` takes the degraded path.
    """
    status_info_ber = b"\x30\x81\x03" + integer(0)
    return tlv(0x30, status_info_ber + token)


def _token(tsa: TsaNode, data: bytes, **kw: object) -> bytes:
    spec = TokenSpec(digest=hashlib.sha256(data).digest(), bare_token=True, **kw)  # type: ignore[arg-type]
    return make_tsr(tsa, spec)


# ---------------------------------------------------------------------------
# The accepted fallback still works for a genuine token
# ---------------------------------------------------------------------------


def test_genuine_ber_token_still_verifies_structurally(tsa: TsaNode) -> None:
    result = check_timestamp(_ber(_token(tsa, ENVELOPE)), ENVELOPE)
    assert result.ok, result.reason
    assert not result.strict
    assert "structural check only" in result.reason


def test_ber_wrapper_is_what_forces_the_degraded_path(tsa: TsaNode) -> None:
    """Guard on the test itself: without the BER byte the same token parses strictly."""
    der = make_tsr(tsa, TokenSpec(digest=hashlib.sha256(ENVELOPE).digest()))
    assert check_timestamp(der, ENVELOPE).strict


# ---------------------------------------------------------------------------
# Forgeries that reached ``timestamp_ok=True`` through the fallback
# ---------------------------------------------------------------------------


def test_signerless_token_with_the_hash_is_rejected() -> None:
    """Granted status + a SignedData-shaped ContentInfo holding only the hash: no signer."""
    content_info = seq(oid(_OID_SIGNED_DATA), tlv(0xA0, seq(octets(hashlib.sha256(ENVELOPE).digest()))))
    forged = seq(seq(integer(0)), content_info)
    result = check_timestamp(forged, ENVELOPE)
    assert result.ok is False
    assert not result.strict
    assert "failed" in result.reason


def test_token_with_no_embedded_certificate_is_rejected(tsa: TsaNode) -> None:
    """A real signature that cannot be checked (no signer certificate) is not a pass."""
    result = check_timestamp(_ber(_token(tsa, ENVELOPE, embed_certs=())), ENVELOPE)
    assert result.ok is False
    assert "signature" in result.reason


def test_token_signed_with_an_unsupported_algorithm_is_rejected() -> None:
    """Ed25519 is not in the fallback's dispatch table: the signature is unverifiable."""
    ca: Node = make_cert("ed-tsa-root", issuer=None, ca=True)
    ed_tsa = make_tsa_cert(ca, cn="ed-tsa", key=ed25519.Ed25519PrivateKey.generate())
    result = check_timestamp(_ber(_token(ed_tsa, ENVELOPE)), ENVELOPE)
    assert result.ok is False
    assert "signature" in result.reason


def test_token_with_an_unknown_signature_oid_is_rejected(tsa: TsaNode) -> None:
    result = check_timestamp(_ber(_token(tsa, ENVELOPE, sig_oid=OID_RSA_PSS)), ENVELOPE)
    assert result.ok is False


def test_imprint_swapped_inside_a_genuine_ber_token_is_rejected(tsa: TsaNode) -> None:
    """S3 revived: a real TSA signature, a TSTInfo rewritten to name another envelope."""
    other = b'{"payload":"what the TSA actually saw"}'
    genuine = _token(tsa, other)
    forged = genuine.replace(hashlib.sha256(other).digest(), hashlib.sha256(ENVELOPE).digest())
    assert forged != genuine
    # The forgery keeps a valid TSA signature: the signed attributes were not touched.
    assert check_timestamp(_ber(genuine), other).ok
    result = check_timestamp(_ber(forged), ENVELOPE)
    assert result.ok is False
    assert "messageDigest" in result.reason


def test_hash_planted_outside_the_tstinfo_is_rejected(tsa: TsaNode) -> None:
    """A genuine token for other data plus the target hash in an extra unsigned field."""
    other = b"other bytes"
    token = _token(tsa, other)
    status_info_ber = b"\x30\x81\x03" + integer(0)
    planted = tlv(0x30, status_info_ber + token + octets(hashlib.sha256(ENVELOPE).digest()))
    result = check_timestamp(planted, ENVELOPE)
    assert result.ok is False


def test_wrong_message_digest_on_the_degraded_path_is_rejected(tsa: TsaNode) -> None:
    result = check_timestamp(_ber(_token(tsa, ENVELOPE, wrong_message_digest=True)), ENVELOPE)
    assert result.ok is False
    assert "messageDigest" in result.reason


def test_missing_message_digest_on_the_degraded_path_is_rejected(tsa: TsaNode) -> None:
    result = check_timestamp(_ber(_token(tsa, ENVELOPE, omit_message_digest=True)), ENVELOPE)
    assert result.ok is False


def test_tampered_signature_on_the_degraded_path_is_rejected(tsa: TsaNode) -> None:
    result = check_timestamp(_ber(_token(tsa, ENVELOPE, tamper_signature=True)), ENVELOPE)
    assert result.ok is False


def test_tstinfo_edited_after_signing_on_the_degraded_path_is_rejected(tsa: TsaNode) -> None:
    result = check_timestamp(_ber(_token(tsa, ENVELOPE, tamper_tstinfo=True)), ENVELOPE)
    assert result.ok is False


@pytest.mark.parametrize("cut", [1, 7, 40, 200])
def test_truncated_ber_token_never_verifies(tsa: TsaNode, cut: int) -> None:
    tsr = _ber(_token(tsa, ENVELOPE))
    assert check_timestamp(tsr[:-cut], ENVELOPE).ok is False


# ---------------------------------------------------------------------------
# End to end: verify_seal_dir and `nova verify --json`
# ---------------------------------------------------------------------------


@pytest.fixture()
def sealed(tmp_path: Path) -> tuple[Path, Path, bytes]:
    """(capsule_dir, novaseal.yaml, dsse_bytes) for a capsule sealed without a TSA."""
    pki = make_pki("degraded")
    key_path = tmp_path / "seal.key"
    cert_path = tmp_path / "seal.crt"
    key_path.write_bytes(pki.leaf.key_pem)
    cert_path.write_bytes(pki.leaf.cert_pem)
    merkle_db = tmp_path / "merkle.db"
    config = tmp_path / "novaseal.yaml"
    config.write_text(
        f"profile: local\nkey_path: {key_path}\ncert_path: {cert_path}\n"
        f"tsa_url: \nmerkle_db: {merkle_db}\n"
    )
    seal = NovaSeal(
        config=KeyConfig(profile="local", key_path=str(key_path), cert_path=str(cert_path)),
        tsa_url="",
        db_path=str(merkle_db),
    )
    bundle = seal.seal({"run_id": "degraded-001", "status": "success"})
    seal_dir = tmp_path / "degraded-capsule" / ".seal"
    seal_dir.mkdir(parents=True)
    (seal_dir / "manifest.dsse").write_bytes(bundle.dsse_envelope)
    (seal_dir / "log-entry.json").write_text(json.dumps(bundle.log_entry), encoding="utf-8")
    return seal_dir.parent, config, bundle.dsse_envelope


def test_verify_seal_dir_reports_a_signerless_token_as_failed(
    sealed: tuple[Path, Path, bytes],
) -> None:
    capsule_dir, _config, dsse = sealed
    content_info = seq(oid(_OID_SIGNED_DATA), tlv(0xA0, seq(octets(hashlib.sha256(dsse).digest()))))
    (capsule_dir / ".seal" / "manifest.dsse.tsr").write_bytes(seq(seq(integer(0)), content_info))
    result = verify_seal_dir(capsule_dir / ".seal")
    assert result.timestamp_present is True
    assert result.timestamp_ok is False
    assert result.valid is False
    assert any("TSR verification failed" in e for e in result.errors)


def test_nova_verify_json_reports_a_swapped_imprint_as_failed(
    sealed: tuple[Path, Path, bytes], tsa: TsaNode
) -> None:
    capsule_dir, config, dsse = sealed
    other = b"bytes the TSA really timestamped"
    forged = _ber(_token(tsa, other)).replace(
        hashlib.sha256(other).digest(), hashlib.sha256(dsse).digest()
    )
    (capsule_dir / ".seal" / "manifest.dsse.tsr").write_bytes(forged)
    result = runner.invoke(
        app, ["verify", str(capsule_dir), "--seal-config", str(config), "--json"]
    )
    assert result.exit_code == 1, result.output
    report = json.loads(result.stdout)
    assert report["timestamp_ok"] is False
    assert report["valid"] is False


def test_nova_verify_json_keeps_the_structural_label_for_a_genuine_ber_token(
    sealed: tuple[Path, Path, bytes], tsa: TsaNode
) -> None:
    capsule_dir, config, dsse = sealed
    (capsule_dir / ".seal" / "manifest.dsse.tsr").write_bytes(_ber(_token(tsa, dsse)))
    result = runner.invoke(
        app, ["verify", str(capsule_dir), "--seal-config", str(config), "--json"]
    )
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert report["timestamp_ok"] is True
    assert report["timestamp_strict"] is False


# ---------------------------------------------------------------------------
# The degraded path's positional readers refuse every malformed shape
# ---------------------------------------------------------------------------

_SHA256 = "2.16.840.1.101.3.4.2.1"
_SHA1 = "1.3.14.3.2.26"
_MD = "1.2.840.113549.1.9.4"
_DATA = "1.2.840.113549.1.7.1"
_TST = "1.2.840.113549.1.9.16.1.4"


def _sd(*children: bytes) -> bytes:
    """A SignedData *value* (the bytes inside the SEQUENCE) from its children."""
    return b"".join(children)


_GOOD_ECI = seq(oid(_TST), tlv(0xA0, octets(b"tst")))


@pytest.mark.parametrize(
    "sd_value",
    [
        pytest.param(_sd(integer(3)), id="too-few-children"),
        pytest.param(_sd(octets(b"x"), tlv(0x31, b""), _GOOD_ECI), id="version-not-integer"),
        pytest.param(_sd(integer(3), tlv(0x31, b""), octets(b"x")), id="eci-not-sequence"),
        pytest.param(_sd(integer(3), tlv(0x31, b""), seq(oid(_TST))), id="eci-one-child"),
        pytest.param(
            _sd(integer(3), tlv(0x31, b""), seq(oid(_DATA), tlv(0xA0, octets(b"t")))),
            id="econtent-type-is-id-data",
        ),
        pytest.param(
            _sd(integer(3), tlv(0x31, b""), seq(oid(_TST), tlv(0xA1, octets(b"t")))),
            id="econtent-wrong-tag",
        ),
        pytest.param(
            _sd(integer(3), tlv(0x31, b""), seq(oid(_TST), tlv(0xA0, seq()))),
            id="econtent-not-octets",
        ),
    ],
)
def test_extract_tst_info_refuses_malformed_signed_data(sd_value: bytes) -> None:
    from novafabric.trust.novaseal.timestamp import _extract_tst_info

    assert _extract_tst_info(sd_value) is None
    assert _extract_tst_info(_sd(integer(3), tlv(0x31, b""), _GOOD_ECI)) == b"tst"


_DIGEST = hashlib.sha256(ENVELOPE).digest()


@pytest.mark.parametrize(
    "tst_der",
    [
        pytest.param(b"", id="empty"),
        pytest.param(octets(b"x"), id="not-a-sequence"),
        pytest.param(seq(integer(1), oid("1.2.3")), id="no-imprint"),
        pytest.param(seq(integer(1), oid("1.2.3"), seq(octets(_DIGEST))), id="imprint-one-child"),
        pytest.param(
            seq(integer(1), oid("1.2.3"), seq(seq(), octets(_DIGEST))), id="empty-algorithm"
        ),
        pytest.param(
            seq(integer(1), oid("1.2.3"), seq(seq(oid(_SHA1)), octets(_DIGEST))),
            id="imprint-algorithm-not-sha256",
        ),
        pytest.param(
            seq(integer(1), oid("1.2.3"), seq(seq(oid(_SHA256)), octets(b"\x00" * 32))),
            id="other-hash",
        ),
    ],
)
def test_tst_imprint_is_read_positionally(tst_der: bytes) -> None:
    from novafabric.trust.novaseal.timestamp import _tst_imprint_matches

    assert _tst_imprint_matches(tst_der, _DIGEST) is False
    good = seq(integer(1), oid("1.2.3"), seq(seq(oid(_SHA256)), octets(_DIGEST)))
    assert _tst_imprint_matches(good, _DIGEST) is True


def _signer_info(*, digest_alg: str = _SHA256, attrs: bytes, sid: bytes | None = None) -> bytes:
    return b"".join(
        [
            integer(1),
            sid if sid is not None else seq(integer(7)),
            seq(oid(digest_alg)),
            tlv(0xA0, attrs),
            seq(oid("1.2.840.10045.4.3.2")),
            octets(b"sig"),
        ]
    )


_TST_BYTES = b"tstinfo-bytes"
_MD_ATTR = seq(oid(_MD), tlv(0x31, octets(hashlib.sha256(_TST_BYTES).digest())))


@pytest.mark.parametrize(
    "si_value",
    [
        pytest.param(integer(1), id="too-short"),
        pytest.param(_signer_info(attrs=_MD_ATTR, sid=octets(b"x")), id="sid-wrong-choice"),
        pytest.param(_signer_info(attrs=_MD_ATTR, digest_alg=_SHA1), id="digest-alg-sha1"),
        pytest.param(_signer_info(attrs=b""), id="no-message-digest"),
        pytest.param(_signer_info(attrs=_MD_ATTR + _MD_ATTR), id="message-digest-twice"),
        pytest.param(
            _signer_info(attrs=seq(oid(_MD), tlv(0x31, octets(b"a") + octets(b"b")))),
            id="message-digest-two-values",
        ),
        pytest.param(_signer_info(attrs=octets(b"x") + _MD_ATTR), id="attribute-not-sequence"),
    ],
)
def test_message_digest_reader_refuses_ambiguous_signer_info(si_value: bytes) -> None:
    from novafabric.trust.novaseal.timestamp import _message_digest_matches

    assert _message_digest_matches(si_value, _TST_BYTES) is None
    assert _message_digest_matches(_signer_info(attrs=_MD_ATTR), _TST_BYTES) is True
    assert _message_digest_matches(_signer_info(attrs=_MD_ATTR), b"other") is False


def test_sha512_digest_algorithm_verifies_on_the_degraded_path(tsa: TsaNode) -> None:
    result = check_timestamp(
        _ber(_token(tsa, ENVELOPE, digest_oid="2.16.840.1.101.3.4.2.3")), ENVELOPE
    )
    assert result.ok, result.reason
    assert not result.strict


def test_response_without_signed_data_fails_on_the_degraded_path() -> None:
    """A granted status and a ContentInfo whose content is not a SignedData SEQUENCE."""
    forged = seq(seq(integer(0)), seq(oid(_OID_SIGNED_DATA), tlv(0xA0, octets(b"x"))))
    result = check_timestamp(forged, ENVELOPE)
    assert result.ok is False
    assert "SignedData" in result.reason
