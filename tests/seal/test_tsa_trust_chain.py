"""``verify_tsa_trust_chain`` — ADR-0070 §1/§2 RFC 3161 TSA trust chain (experimental).

Acceptance criteria:

* A token signed by a TSA certificate with a critical, timeStamping-only EKU that
  chains to the operator TSA CA verifies (ECDSA, RSA, Ed25519; ESSCertID v1 and v2;
  sid by issuer+serial and by subject key identifier; bare token and TimeStampResp).
* The real freetsa.org fixture verifies against its own embedded root, offline.
* Fail closed on: wrong CA, missing / non-critical / extra-purpose EKU, keyUsage
  without digitalSignature/nonRepudiation, tampered TSTInfo, bad signature, missing
  or wrong messageDigest / contentType, ESSCertID pointing at another certificate or
  serial, message-imprint mismatch, revoked TSA certificate (CRL), signer certificate
  absent, SHA-1 / unsupported algorithms, malformed or oversize input, rejected status.
* The chain and CRL are evaluated at genTime: a TSA certificate expired *now* still
  validates an older token; a revocation after genTime does not count — unless it is
  for keyCompromise / cACompromise (retroactive). CRL *freshness* is judged at
  verification time, so ``crl_strict`` works with a CRL synced after genTime.
* The message imprint must use the expected hash algorithm, not just match bytes.
"""

from __future__ import annotations

import datetime
import hashlib
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID

from novafabric.trust.novaseal.tsa_token import MAX_TOKEN_BYTES, parse_timestamp_token
from novafabric.trust.novaseal.tsa_trust import TSA_MAX_CHAIN_DEPTH, verify_tsa_trust_chain

from ._tsa_pki import (
    OID_DATA,
    OID_ED25519,
    OID_RSA_ENCRYPTION,
    OID_RSA_PSS,
    OID_SHA1,
    OID_SHA512,
    TokenSpec,
    TsaNode,
    make_tsa_cert,
    make_tsr,
)
from ._x509_pki import NOW, Node, Revoked, crl_der, make_cert, make_crl

DAY = datetime.timedelta(days=1)
DIGEST = hashlib.sha256(b"dsse").digest()
FREETSA = Path(__file__).parent.parent / "fixtures" / "rfc3161" / "freetsa-response.tsr"


@pytest.fixture(scope="module")
def ca() -> Node:
    return make_cert("tsa-test-root", issuer=None, ca=True)


@pytest.fixture(scope="module")
def tsa(ca: Node) -> TsaNode:
    return make_tsa_cert(ca)


def _verify(token: bytes, ca: Node, **kw: object):  # type: ignore[no-untyped-def]
    return verify_tsa_trust_chain(token, ca.cert_pem, expected_digest=DIGEST, **kw)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Good tokens
# ---------------------------------------------------------------------------


def test_good_token_verifies(ca: Node, tsa: TsaNode) -> None:
    result = _verify(make_tsr(tsa), ca)
    assert result.valid, result.reason
    assert result.signer_subject == "CN=novaseal-test-tsa"
    assert result.chain_subjects == ("CN=novaseal-test-tsa", "CN=tsa-test-root")
    assert result.ess_cert_id_version == 2
    assert result.gen_time == NOW
    assert result.imprint_digest_hex == DIGEST.hex()
    assert result.nonce == 0x1234567890ABCDEF
    assert result.policy == "1.2.3.4.1"
    assert result.trust_anchor_fingerprint
    assert result.revocation is None


@pytest.mark.parametrize(
    "spec",
    [
        TokenSpec(ess_version=1),
        TokenSpec(ess_hash_oid=OID_SHA512),
        TokenSpec(sid_by_key_id=True),
        TokenSpec(bare_token=True),
        TokenSpec(status=1),
        TokenSpec(nonce=None),
        TokenSpec(digest_oid=OID_SHA512),
        TokenSpec(ess_serial=None),
    ],
    ids=[
        "ess-v1",
        "ess-v2-sha512",
        "sid-ski",
        "bare",
        "granted-mods",
        "no-nonce",
        "sha512",
        "no-issuer-serial",
    ],
)
def test_good_token_variants(ca: Node, tsa: TsaNode, spec: TokenSpec) -> None:
    result = _verify(make_tsr(tsa, spec), ca)
    assert result.valid, result.reason


def test_ess_issuer_serial_matching_passes(ca: Node, tsa: TsaNode) -> None:
    spec = TokenSpec(ess_serial=tsa.cert.serial_number)
    assert _verify(make_tsr(tsa, spec), ca).valid


@pytest.mark.parametrize(
    ("key", "sig_oid"),
    [
        (rsa.generate_private_key(65537, 2048), None),
        (rsa.generate_private_key(65537, 2048), OID_RSA_ENCRYPTION),
        (ed25519.Ed25519PrivateKey.generate(), OID_ED25519),
    ],
    ids=["rsa-sha256", "rsa-bare", "ed25519"],
)
def test_other_key_types(ca: Node, key: object, sig_oid: str | None) -> None:
    node = make_tsa_cert(ca, key=key)  # type: ignore[arg-type]
    result = _verify(make_tsr(node, TokenSpec(sig_oid=sig_oid)), ca)
    assert result.valid, result.reason


def test_intermediate_from_token_builds_path(ca: Node) -> None:
    inter = make_cert("tsa-test-intermediate", issuer=ca, ca=True)
    node = make_tsa_cert(inter)
    token = make_tsr(node, TokenSpec(embed_certs=(node.cert, inter.cert)))
    result = _verify(token, ca)
    assert result.valid, result.reason
    assert len(result.chain_subjects) == 3
    # Without the intermediate anywhere, the path cannot be built.
    assert not _verify(make_tsr(node), ca).valid


def test_signer_supplied_out_of_band(ca: Node, tsa: TsaNode) -> None:
    token = make_tsr(tsa, TokenSpec(embed_certs=()))
    missing = _verify(token, ca)
    assert not missing.valid and "not embedded" in missing.reason
    assert _verify(token, ca, untrusted_certs=[tsa.cert]).valid


def test_anchors_as_certificate_list(ca: Node, tsa: TsaNode) -> None:
    assert verify_tsa_trust_chain(make_tsr(tsa), [ca.cert]).valid


def test_real_freetsa_token_verifies_offline() -> None:
    data = FREETSA.read_bytes()
    token = parse_timestamp_token(data)
    root = [c for c in token.certificates if c.issuer == c.subject]
    result = verify_tsa_trust_chain(data, root)
    assert result.valid, result.reason
    assert result.ess_cert_id_version == 1
    assert result.nonce == 12473090696047252391
    assert "CN=www.freetsa.org" in result.signer_subject  # type: ignore[operator]
    strict = verify_tsa_trust_chain(data, root, require_ess_cert_id_v2=True)
    assert not strict.valid and "ESSCertIDv2" in strict.reason


# ---------------------------------------------------------------------------
# Failure cases (fail closed)
# ---------------------------------------------------------------------------


def test_wrong_ca_fails(tsa: TsaNode) -> None:
    other = make_cert("some-other-root", issuer=None, ca=True)
    result = verify_tsa_trust_chain(make_tsr(tsa), other.cert_pem)
    assert not result.valid
    assert "chain validation failed" in result.reason


def test_self_signed_tsa_not_in_bundle_fails(ca: Node) -> None:
    rogue_ca = make_cert("rogue", issuer=None, ca=True)
    rogue = make_tsa_cert(rogue_ca, cn="novaseal-test-tsa")
    assert not _verify(
        make_tsr(rogue, TokenSpec(embed_certs=(rogue.cert, rogue_ca.cert))), ca
    ).valid


@pytest.mark.parametrize(
    ("kwargs", "fragment"),
    [
        ({"eku": None}, "no extendedKeyUsage"),
        ({"eku_critical": False}, "must be critical"),
        ({"eku": (ExtendedKeyUsageOID.CODE_SIGNING,)}, "only id-kp-timeStamping"),
        (
            {"eku": (ExtendedKeyUsageOID.TIME_STAMPING, ExtendedKeyUsageOID.CODE_SIGNING)},
            "only id-kp-timeStamping",
        ),
        ({"digital_signature": False}, "keyUsage"),
    ],
    ids=["missing", "non-critical", "wrong", "extra", "key-usage"],
)
def test_eku_and_key_usage_enforced(ca: Node, kwargs: dict[str, object], fragment: str) -> None:
    node = make_tsa_cert(ca, **kwargs)  # type: ignore[arg-type]
    result = _verify(make_tsr(node), ca)
    assert not result.valid
    assert fragment in result.reason


@pytest.mark.parametrize(
    ("spec", "fragment"),
    [
        (TokenSpec(tamper_tstinfo=True), "messageDigest attribute does not match"),
        (TokenSpec(wrong_message_digest=True), "messageDigest attribute does not match"),
        (TokenSpec(tamper_signature=True), "does not verify"),
        (TokenSpec(omit_message_digest=True), "lack messageDigest"),
        (TokenSpec(omit_content_type=True), "contentType"),
        (TokenSpec(content_type_oid=OID_DATA), "contentType"),
        (TokenSpec(ess_version=None), "signingCertificate"),
        (TokenSpec(ess_serial=12345), "issuerSerial"),
        (TokenSpec(ess_hash_oid=OID_SHA1), "unsupported ESSCertIDv2 hash"),
        (TokenSpec(digest_oid=OID_SHA1), "SHA-1 is not accepted"),
        (TokenSpec(sig_oid=OID_RSA_PSS), "unsupported CMS signature algorithm"),
        (TokenSpec(sig_oid=OID_ED25519), "not Ed25519"),
        (TokenSpec(sig_oid=OID_RSA_ENCRYPTION), "not RSA"),
    ],
    ids=[
        "tampered-tstinfo",
        "wrong-md",
        "bad-sig",
        "no-md",
        "no-ct",
        "wrong-ct",
        "no-ess",
        "ess-serial",
        "ess-sha1",
        "sha1-digest",
        "pss",
        "ed-mismatch",
        "rsa-mismatch",
    ],
)
def test_token_integrity_failures(ca: Node, tsa: TsaNode, spec: TokenSpec, fragment: str) -> None:
    result = _verify(make_tsr(tsa, spec), ca)
    assert not result.valid
    assert fragment in result.reason, result.reason


def test_ecdsa_oid_with_rsa_key_fails(ca: Node) -> None:
    node = make_tsa_cert(ca, key=rsa.generate_private_key(65537, 2048))
    result = _verify(make_tsr(node, TokenSpec(sig_oid="1.2.840.10045.4.3.2")), ca)
    assert not result.valid and "not EC" in result.reason


def test_ess_cert_id_for_another_certificate_fails(ca: Node, tsa: TsaNode) -> None:
    decoy = make_tsa_cert(ca, cn="decoy")
    for version in (1, 2):
        result = _verify(make_tsr(tsa, TokenSpec(ess_version=version, ess_cert=decoy.cert)), ca)
        assert not result.valid
        assert "certificate hash does not match" in result.reason


def test_digest_mismatch_fails(ca: Node, tsa: TsaNode) -> None:
    token = make_tsr(tsa, TokenSpec(digest=hashlib.sha256(b"other data").digest()))
    result = _verify(token, ca)
    assert not result.valid
    assert "messageImprint does not match" in result.reason
    # Without an expected digest the imprint is only reported.
    assert verify_tsa_trust_chain(token, ca.cert_pem).valid


def test_imprint_length_must_match_algorithm(ca: Node, tsa: TsaNode) -> None:
    token = make_tsr(tsa, TokenSpec(digest=b"\x01" * 20))
    result = verify_tsa_trust_chain(token, ca.cert_pem, expected_digest=b"\x01" * 20)
    assert not result.valid and "length" in result.reason


def test_revoked_tsa_cert_fails(tmp_path: Path, ca: Node, tsa: TsaNode) -> None:
    crls = tmp_path / "crls"
    crls.mkdir()
    (crls / "root.crl").write_bytes(
        crl_der(
            make_crl(
                ca,
                (Revoked(tsa.cert.serial_number, NOW - DAY, x509.ReasonFlags.key_compromise),),
                this_update=NOW - DAY,
            )
        )
    )
    result = _verify(make_tsr(tsa), ca, crl_cache_dir=crls)
    assert not result.valid
    assert "revocation" in result.reason
    assert result.revocation is not None and not result.revocation.ok


def test_fresh_crl_is_good_and_revocation_after_gen_time_is_ignored(
    tmp_path: Path, ca: Node, tsa: TsaNode
) -> None:
    crls = tmp_path / "crls"
    crls.mkdir()
    (crls / "root.crl").write_bytes(
        crl_der(
            make_crl(
                ca,
                (Revoked(tsa.cert.serial_number, NOW + datetime.timedelta(hours=1)),),
                this_update=NOW - DAY,
            )
        )
    )
    result = _verify(make_tsr(tsa), ca, crl_cache_dir=crls)
    assert result.valid, result.reason
    assert result.revocation is not None
    assert result.revocation.certificates[0].status.value == "good"


def test_missing_crl_soft_then_strict(tmp_path: Path, ca: Node, tsa: TsaNode) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    assert _verify(make_tsr(tsa), ca, crl_cache_dir=empty).valid
    strict = _verify(make_tsr(tsa), ca, crl_cache_dir=empty, crl_strict=True)
    assert not strict.valid


def test_unusable_crl_dir_fails_closed(tmp_path: Path, ca: Node, tsa: TsaNode) -> None:
    result = _verify(make_tsr(tsa), ca, crl_cache_dir=tmp_path / "missing")
    assert not result.valid and "CRL directory" in result.reason


def test_validation_at_gen_time() -> None:
    """A TSA cert that expired since still validates a token issued while it was valid."""
    ca = make_cert("tsa-old-root", issuer=None, ca=True, not_before=NOW - 60 * DAY)
    old = make_tsa_cert(ca, not_before=NOW - 30 * DAY, not_after=NOW - 10 * DAY)
    assert _verify(make_tsr(old, TokenSpec(gen_time=NOW - 20 * DAY)), ca).valid
    late = _verify(make_tsr(old, TokenSpec(gen_time=NOW - 5 * DAY)), ca)
    assert not late.valid


def test_bad_bundles_fail(tsa: TsaNode) -> None:
    assert "unusable" in verify_tsa_trust_chain(make_tsr(tsa), b"not pem").reason
    assert "no trust anchors" in verify_tsa_trust_chain(make_tsr(tsa), []).reason


@pytest.mark.parametrize(
    ("spec", "fragment"),
    [
        (TokenSpec(status=2), "not granted"),
        (TokenSpec(signer_infos=2), "exactly one SignerInfo"),
        (TokenSpec(econtent_type_oid=OID_DATA), "id-ct-TSTInfo"),
    ],
)
def test_structural_rejections(ca: Node, tsa: TsaNode, spec: TokenSpec, fragment: str) -> None:
    result = _verify(make_tsr(tsa, spec), ca)
    assert not result.valid
    assert result.reason.startswith("malformed time-stamp token") and fragment in result.reason


def test_malformed_inputs_never_raise(ca: Node, tsa: TsaNode) -> None:
    good = make_tsr(tsa)
    for blob in (b"", b"\x30", good[:-3], good + b"\x00", b"\x02\x01\x00", b"\x00" * 10):
        result = verify_tsa_trust_chain(blob, ca.cert_pem)
        assert not result.valid and result.reason.startswith("malformed")
    oversize = verify_tsa_trust_chain(b"\x30" * (MAX_TOKEN_BYTES + 1), ca.cert_pem)
    assert not oversize.valid and "larger than" in oversize.reason


def test_argument_bounds(ca: Node, tsa: TsaNode) -> None:
    with pytest.raises(ValueError, match="max_chain_depth"):
        verify_tsa_trust_chain(make_tsr(tsa), ca.cert_pem, max_chain_depth=TSA_MAX_CHAIN_DEPTH + 1)
    with pytest.raises(ValueError, match="untrusted_certs"):
        verify_tsa_trust_chain(make_tsr(tsa), ca.cert_pem, untrusted_certs=[tsa.cert] * 33)


def test_non_timestamp_ca_policy_still_requires_ca_flag(ca: Node) -> None:
    """The relaxed TSA CA profile tolerates non-critical basicConstraints, not cA=FALSE."""
    not_ca = make_tsa_cert(ca, cn="not-a-ca", eku=None)
    fake_issuer = Node(cert=not_ca.cert, key=not_ca.key)  # type: ignore[arg-type]
    leaf = make_tsa_cert(fake_issuer)
    token = make_tsr(leaf, TokenSpec(embed_certs=(leaf.cert, not_ca.cert)))
    assert not _verify(token, ca).valid


def test_ec_key_fixture_type(tsa: TsaNode) -> None:
    assert isinstance(tsa.key, ec.EllipticCurvePrivateKey)


def test_minimal_tsa_cert_and_ski_lookup_skips_certs_without_ski(ca: Node) -> None:
    """No keyUsage is fine; an SKI-addressed signer is not matched by an SKI-less cert."""
    minimal = make_tsa_cert(ca, minimal=True)
    assert _verify(make_tsr(minimal), ca).valid
    full = make_tsa_cert(ca)
    token = make_tsr(full, TokenSpec(sid_by_key_id=True, embed_certs=(minimal.cert, full.cert)))
    assert _verify(token, ca).valid
    orphan = make_tsr(full, TokenSpec(sid_by_key_id=True, embed_certs=(minimal.cert,)))
    assert "not embedded" in _verify(orphan, ca).reason


def test_require_ca_validator() -> None:
    from novafabric.trust.novaseal.tsa_trust import _require_ca

    with pytest.raises(ValueError, match="cA=TRUE"):
        _require_ca(None, None, None)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="cA=TRUE"):
        _require_ca(None, None, x509.BasicConstraints(ca=False, path_length=None))  # type: ignore[arg-type]
    _require_ca(None, None, x509.BasicConstraints(ca=True, path_length=None))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Revocation as of genTime, CRL freshness judged now (review defect 6)
# ---------------------------------------------------------------------------

GEN_TIME = NOW - 3 * DAY


@pytest.fixture(scope="module")
def old_ca() -> Node:
    return make_cert("tsa-pit-root", issuer=None, ca=True, not_before=NOW - 60 * DAY)


@pytest.fixture(scope="module")
def old_tsa(old_ca: Node) -> TsaNode:
    """A TSA certificate valid well before ``GEN_TIME``."""
    return make_tsa_cert(old_ca, not_before=NOW - 30 * DAY)


def _crl_dir(tmp_path: Path, ca: Node, *revoked: Revoked) -> Path:
    """A CRL issued an hour ago — i.e. *after* the token's genTime, current now."""
    crls = tmp_path / "crls"
    crls.mkdir()
    (crls / "root.crl").write_bytes(crl_der(make_crl(ca, revoked)))
    return crls


def test_strict_crl_issued_after_gen_time_passes_for_good_cert(
    tmp_path: Path, old_ca: Node, old_tsa: TsaNode
) -> None:
    token = make_tsr(old_tsa, TokenSpec(gen_time=GEN_TIME))
    result = _verify(token, old_ca, crl_cache_dir=_crl_dir(tmp_path, old_ca), crl_strict=True)
    assert result.valid, result.reason
    assert result.revocation is not None and result.revocation.strict
    assert [c.status.value for c in result.revocation.certificates] == ["good"]


def test_key_compromise_after_gen_time_revokes(
    tmp_path: Path, old_ca: Node, old_tsa: TsaNode
) -> None:
    entry = Revoked(old_tsa.cert.serial_number, NOW - DAY, x509.ReasonFlags.key_compromise)
    token = make_tsr(old_tsa, TokenSpec(gen_time=GEN_TIME))
    result = _verify(token, old_ca, crl_cache_dir=_crl_dir(tmp_path, old_ca, entry))
    assert not result.valid and "revocation" in result.reason
    assert result.revocation is not None
    assert result.revocation.certificates[0].status.value == "revoked"
    assert result.revocation.certificates[0].reason == "key_compromise"


def test_superseded_after_gen_time_does_not_revoke(
    tmp_path: Path, old_ca: Node, old_tsa: TsaNode
) -> None:
    entry = Revoked(old_tsa.cert.serial_number, NOW - DAY, x509.ReasonFlags.superseded)
    token = make_tsr(old_tsa, TokenSpec(gen_time=GEN_TIME))
    result = _verify(
        token, old_ca, crl_cache_dir=_crl_dir(tmp_path, old_ca, entry), crl_strict=True
    )
    assert result.valid, result.reason
    # ... but superseded *before* genTime does revoke.
    early = Revoked(old_tsa.cert.serial_number, GEN_TIME - DAY, x509.ReasonFlags.superseded)
    other = tmp_path / "other"
    other.mkdir()
    (other / "root.crl").write_bytes(crl_der(make_crl(old_ca, (early,))))
    assert not _verify(token, old_ca, crl_cache_dir=other).valid


def test_crl_stale_at_verification_time_fails_strict(
    tmp_path: Path, old_ca: Node, old_tsa: TsaNode
) -> None:
    """A CRL that was current at genTime but has lapsed since is stale (strict fails)."""
    crls = tmp_path / "crls"
    crls.mkdir()
    lapsed = make_crl(old_ca, this_update=GEN_TIME - DAY, next_update=NOW - DAY)
    (crls / "root.crl").write_bytes(crl_der(lapsed))
    token = make_tsr(old_tsa, TokenSpec(gen_time=GEN_TIME))
    assert _verify(token, old_ca, crl_cache_dir=crls).valid  # soft-fail warning
    strict = _verify(token, old_ca, crl_cache_dir=crls, crl_strict=True)
    assert not strict.valid
    assert strict.revocation is not None
    assert strict.revocation.certificates[0].status.value == "stale"
    # An explicit (naive = UTC) verification time inside the CRL window passes strict.
    at_gen = _verify(
        token,
        old_ca,
        crl_cache_dir=crls,
        crl_strict=True,
        verification_time=GEN_TIME.replace(tzinfo=None),
    )
    assert at_gen.valid, at_gen.reason


# ---------------------------------------------------------------------------
# messageImprint hash algorithm (review defect 8)
# ---------------------------------------------------------------------------


def test_imprint_algorithm_must_match_expected(ca: Node, tsa: TsaNode) -> None:
    # Same bytes, but the TSA attested them as a SHA-512 (or unknown) digest.
    for imprint_oid in (OID_SHA512, "1.2.3.4.5", OID_SHA1):
        token = make_tsr(tsa, TokenSpec(imprint_oid=imprint_oid))
        result = _verify(token, ca)
        assert not result.valid
        assert "messageImprint hash algorithm" in result.reason
        assert result.imprint_algorithm == imprint_oid


def test_imprint_with_explicit_sha512(ca: Node, tsa: TsaNode) -> None:
    digest = hashlib.sha512(b"dsse").digest()
    token = make_tsr(tsa, TokenSpec(imprint_oid=OID_SHA512, digest=digest))
    ok = verify_tsa_trust_chain(
        token, ca.cert_pem, expected_digest=digest, expected_digest_algorithm=OID_SHA512
    )
    assert ok.valid, ok.reason
    assert not verify_tsa_trust_chain(token, ca.cert_pem, expected_digest=digest).valid
    with pytest.raises(ValueError, match="expected_digest_algorithm"):
        verify_tsa_trust_chain(token, ca.cert_pem, expected_digest_algorithm=OID_SHA1)
