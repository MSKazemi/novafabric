"""Strict RFC 3161 token parser (``tsa_token``) — ADR-0070 §1 (experimental).

Acceptance criteria: every malformed, BER-only, oversize or out-of-profile structure
raises ``TsaTokenError`` (never another exception, never a silent partial parse);
well-formed tokens — hand-built and the real freetsa.org fixture — parse to the
expected fields.
"""

from __future__ import annotations

import datetime
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.serialization import Encoding

from novafabric.trust.novaseal import tsa_token as tt
from novafabric.trust.novaseal.tsa_token import TsaTokenError, parse_timestamp_token

from ._tsa_pki import (
    OID_SHA256,
    OID_TST_INFO,
    TokenSpec,
    alg,
    der_set,
    gentime,
    integer,
    make_tsa_cert,
    make_tsr,
    octets,
    oid,
    seq,
    tlv,
)
from ._x509_pki import NOW, make_cert

FREETSA = Path(__file__).parent.parent / "fixtures" / "rfc3161" / "freetsa-response.tsr"
_SIGNED_DATA = "1.2.840.113549.1.7.2"


def _tlv(raw: bytes) -> tt._Tlv:
    return tt._single(raw, "test")


def _bad(fn: object, *args: object, match: str) -> None:
    with pytest.raises(TsaTokenError, match=match):
        fn(*args)  # type: ignore[operator]


# ---------------------------------------------------------------------------
# DER primitives
# ---------------------------------------------------------------------------


def test_read_rejects_ber_and_bad_lengths() -> None:
    _bad(tt._read, b"\x1f\x01\x00", 0, match="high-tag")
    _bad(tt._read, b"\x30\x80\x00\x00", 0, match="indefinite")
    _bad(tt._read, b"\x30\x85\x00\x00\x00\x00\x01", 0, match="invalid DER length")
    _bad(tt._read, b"\x30\x82\x00", 0, match="invalid DER length")
    _bad(tt._read, b"\x30\x05\x00", 0, match="past the end")
    _bad(tt._read, b"\x30", 0, match="truncated")
    # Long form for a short length is BER, not DER (formerly accepted).
    _bad(tt._read, b"\x04\x81\x01\xaa", 0, match="non-minimal")


def test_read_rejects_non_minimal_lengths() -> None:
    """X.690 §10.1: long form only for lengths >= 128, and no leading zero octet."""
    _bad(tt._read, b"\x04\x81\x05" + b"a" * 5, 0, match="long form for a length < 128")
    _bad(tt._read, b"\x04\x81\x00", 0, match="leading zero")
    _bad(tt._read, b"\x04\x82\x00\x80" + b"a" * 128, 0, match="leading zero")
    _bad(tt._read, b"\x04\x82\x00\x05" + b"a" * 5, 0, match="leading zero")
    tlv_ok, end = tt._read(b"\x04\x81\x80" + b"a" * 128, 0)
    assert end == 131 and tlv_ok.value == b"a" * 128
    tlv_ok, end = tt._read(b"\x04\x82\x01\x00" + b"a" * 256, 0)
    assert end == 260 and len(tlv_ok.value) == 256


def test_token_with_re_encoded_length_is_rejected() -> None:
    """Re-encoding an outer length non-minimally must not yield a second valid token."""
    good = make_tsr(make_tsa_cert(make_cert("der-root", issuer=None, ca=True)))
    assert good[0] == 0x30 and good[1] == 0x82  # outer TimeStampResp, 2-byte length
    malleated = b"\x30\x83\x00" + good[2:]
    with pytest.raises(TsaTokenError, match="non-minimal"):
        parse_timestamp_token(malleated)
    parse_timestamp_token(good)


def test_children_bounds() -> None:
    _bad(tt._children, _tlv(b"\x04\x00"), "x", match="constructed")
    many = seq(*([b"\x05\x00"] * 257))
    _bad(tt._children, _tlv(many), "x", match="more than 256")


def test_integer_and_oid() -> None:
    _bad(tt._integer, _tlv(b"\x02\x00"), "n", match="empty INTEGER")
    _bad(tt._integer, _tlv(b"\x04\x00"), "n", match="expected tag")
    assert tt._integer(_tlv(integer(-5)), "n") == -5
    _bad(tt._oid, _tlv(b"\x06\x00"), "o", match="malformed")
    _bad(tt._oid, _tlv(b"\x06\x01\x81"), "o", match="malformed")
    _bad(tt._oid, _tlv(b"\x06\x02\x2a\x80"), "o", match="malformed")
    _bad(tt._oid, _tlv(b"\x06\x03\x2a\x80\x01"), "o", match="non-minimal")
    assert tt._oid(_tlv(oid(OID_SHA256)), "o") == OID_SHA256
    assert tt._oid(_tlv(oid("2.999.3")), "o") == "2.999.3"


def test_algorithm_identifier() -> None:
    _bad(tt._algorithm, _tlv(seq()), "a", match="malformed AlgorithmIdentifier")
    got = tt._algorithm(_tlv(alg(OID_SHA256)), "a")
    assert got.oid == OID_SHA256 and got.parameters == b"\x05\x00"
    assert tt._algorithm(_tlv(alg(OID_SHA256, null=False)), "a").parameters is None


def test_gen_time() -> None:
    assert tt._gen_time(_tlv(tlv(0x18, b"20260827173122.25Z"))) == datetime.datetime(
        2026, 8, 27, 17, 31, 22, 250000, tzinfo=datetime.timezone.utc
    )
    _bad(tt._gen_time, _tlv(tlv(0x18, b"20260827173122+0100")), match="UTC GeneralizedTime")
    _bad(tt._gen_time, _tlv(tlv(0x18, b"20261340173122Z")), match="invalid date")


# ---------------------------------------------------------------------------
# TSTInfo / attributes / SignerInfo / SignedData
# ---------------------------------------------------------------------------


def _tst(*extra: bytes, imprint: bytes | None = None) -> bytes:
    return seq(
        integer(1),
        oid("1.2.3"),
        imprint if imprint is not None else seq(alg(OID_SHA256), octets(b"\x00" * 32)),
        integer(7),
        gentime(NOW),
        *extra,
    )


def test_tst_info_fields() -> None:
    info = tt._tst_info(_tst(seq(integer(1)), tlv(0x01, b"\xff"), integer(99), tlv(0xA0, b"")))
    assert info.nonce == 99 and info.serial_number == 7 and info.policy == "1.2.3"
    _bad(tt._tst_info, seq(integer(1)), match="missing mandatory")
    _bad(tt._tst_info, _tst(imprint=seq(alg(OID_SHA256))), match="messageImprint: malformed")
    _bad(tt._tst_info, _tst(integer(1), integer(2)), match="second INTEGER")
    _bad(tt._tst_info, _tst(octets(b"x")), match="unexpected field")
    _bad(tt._tst_info, _tst() + b"\x00", match="trailing bytes")


def _ess(version: int, *ids: bytes, policies: bytes = b"") -> tt._Tlv:
    return _tlv(seq(seq(*ids), policies) if ids or not policies else seq(seq(), policies))


def test_ess_cert_id_shapes() -> None:
    v1 = tt._ess_cert_id(_ess(1, seq(octets(b"h" * 20))), 1)
    assert v1.version == 1 and v1.issuer_serial is None
    v2 = tt._ess_cert_id(_ess(2, seq(octets(b"h" * 32), seq(seq(), integer(5)))), 2)
    assert v2.hash_algorithm.oid == OID_SHA256 and v2.issuer_serial == 5
    _bad(tt._ess_cert_id, _tlv(seq()), 1, match="SigningCertificate: malformed")
    _bad(tt._ess_cert_id, _tlv(seq(seq())), 2, match="empty certs")
    _bad(tt._ess_cert_id, _ess(1, seq()), 1, match="ESSCertID: malformed")
    _bad(
        tt._ess_cert_id,
        _ess(1, seq(octets(b"h"), seq(integer(1)))),
        1,
        match="IssuerSerial: malformed",
    )


def _attrs(*attrs: bytes) -> tt._Tlv:
    return _tlv(tlv(0xA0, b"".join(attrs)))


def test_signed_attribute_rules() -> None:
    ct = seq(oid(tt.OID_CONTENT_TYPE), der_set(oid(OID_TST_INFO)))
    _bad(tt._signed_attributes, _attrs(seq(oid("1.2"))), match="Attribute: malformed")
    _bad(tt._signed_attributes, _attrs(ct, ct), match="duplicate")
    two = seq(oid(tt.OID_CONTENT_TYPE), der_set(oid("1.2"), oid("1.3")))
    _bad(tt._signed_attributes, _attrs(two), match="exactly one value")
    other = seq(oid("1.2.840.113549.1.9.5"), der_set(tlv(0x17, b"260827173122Z")))
    content_type, digest, ess = tt._signed_attributes(_attrs(ct, other))
    assert content_type == OID_TST_INFO and digest is None and ess is None


def _signer(*fields: bytes) -> tt._Tlv:
    return _tlv(seq(*fields))


def test_signer_info_shapes() -> None:
    sid = seq(seq(), integer(1))
    attrs = tlv(0xA0, seq(oid(tt.OID_CONTENT_TYPE), der_set(oid(OID_TST_INFO))))
    base = [integer(1), sid, alg(OID_SHA256), attrs, alg(OID_SHA256), octets(b"s")]
    assert tt._signer_info(_signer(*base)).sid_serial == 1
    assert tt._signer_info(_signer(*base, tlv(0xA1, b""))).signature == b"s"
    _bad(tt._signer_info, _signer(*base[:5]), match="signed attributes are mandatory")
    no_attrs = [*base[:3], alg(OID_SHA256), octets(b"s"), tlv(0xA1, b"")]
    _bad(tt._signer_info, _signer(*no_attrs), match="signed attributes are missing")
    _bad(tt._signer_info, _signer(*base, octets(b"x")), match="unexpected trailing")
    bad_sid = [base[0], seq(integer(1)), *base[2:]]
    _bad(tt._signer_info, _signer(*bad_sid), match="IssuerAndSerialNumber")
    other_sid = [base[0], octets(b"x"), *base[2:]]
    _bad(tt._signer_info, _signer(*other_sid), match="unsupported choice")


def _content_info(signed_data: bytes, content_type: str = _SIGNED_DATA) -> bytes:
    return seq(oid(content_type), tlv(0xA0, signed_data))


def test_signed_data_shapes() -> None:
    ca = make_cert("tok-root", issuer=None, ca=True)
    tsa = make_tsa_cert(ca)
    good = parse_timestamp_token(make_tsr(tsa, TokenSpec(bare_token=True)))
    assert good.pki_status is None and len(good.certificates) == 1

    _bad(tt._signed_data, _tlv(_content_info(seq(), "1.2.3")), match="not a CMS SignedData")
    _bad(tt._signed_data, _tlv(seq(oid(_SIGNED_DATA), tlv(0xA0, b""))), match="content: malformed")
    _bad(tt._signed_data, _tlv(_content_info(seq(integer(3)))), match="missing mandatory")
    encap_bad = seq(oid(OID_TST_INFO), tlv(0xA0, octets(b"a") + octets(b"b")))
    sd = seq(integer(3), der_set(), encap_bad, der_set())
    _bad(tt._signed_data, _tlv(_content_info(sd)), match="eContent: malformed")
    encap = seq(oid(OID_TST_INFO), tlv(0xA0, octets(_tst())))
    _bad(
        tt._signed_data,
        _tlv(_content_info(seq(integer(3), der_set(), encap))),
        match="missing mandatory",
    )
    sd_extra = seq(integer(3), der_set(), encap, tlv(0xA1, b""), der_set(), der_set())
    _bad(tt._signed_data, _tlv(_content_info(sd_extra)), match="malformed trailing")
    # crls [1] is skipped; zero SignerInfos is rejected.
    sd_crls = seq(integer(3), der_set(), encap, tlv(0xA1, b""), der_set())
    _bad(tt._signed_data, _tlv(_content_info(sd_crls)), match="exactly one SignerInfo, got 0")


def test_certificate_choices() -> None:
    ca = make_cert("tok-root2", issuer=None, ca=True)
    der = ca.cert.public_bytes(Encoding.DER)
    certs = tt._certificates(_tlv(tlv(0xA0, der + tlv(0xA1, b""))))
    assert len(certs) == 1  # the [1] attribute-certificate choice is skipped
    _bad(tt._certificates, _tlv(tlv(0xA0, der * 33)), match="more than 32")
    _bad(tt._certificates, _tlv(tlv(0xA0, seq(integer(1)))), match="unparseable certificate")


def test_timestamp_resp_shapes() -> None:
    _bad(parse_timestamp_token, seq(seq()), match="PKIStatusInfo: empty")
    _bad(parse_timestamp_token, seq(seq(integer(0))), match="no TimeStampToken")
    _bad(parse_timestamp_token, seq(seq(integer(3)), seq()), match="not granted")
    _bad(parse_timestamp_token, octets(b""), match="expected tag")


def test_parses_real_freetsa_fixture() -> None:
    token = parse_timestamp_token(FREETSA.read_bytes())
    assert token.pki_status == 0
    assert token.tst_info.nonce == 12473090696047252391
    assert token.tst_info.serial_number == 0x074F5985
    assert token.signer.digest_algorithm.oid == "2.16.840.1.101.3.4.2.3"
    assert token.signer.ess_cert_id is not None and token.signer.ess_cert_id.version == 1
    assert len(token.certificates) == 2
