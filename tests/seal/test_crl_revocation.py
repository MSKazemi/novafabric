"""Offline CRL revocation checking — ADR-0070 §3 / ADR-0055 OQ-55-3 (experimental).

Acceptance criteria:

* CRLs (DER or PEM) load from an operator directory; bounded (too many files fails
  closed, oversize files are skipped with a finding); non-CRL, delta, indirect,
  partitioned and unknown-critical-extension CRLs are skipped with a finding; hidden
  files and sub-directories are ignored. Nothing is ever fetched.
* Each non-anchor certificate of the validated path is checked against the CRL whose
  issuer is that certificate's issuer, authenticated with the issuer's public key.
* Outcomes: good / revoked (reason + date) / no_crl / stale / invalid_crl.
  A forged CRL (wrong signing key) can neither revoke nor clear a certificate.
* Policy: revoked always fails; no_crl/stale/invalid_crl warn by default and fail
  under ``strict``.
* Wiring: ``validate_certificate_chain`` / ``verify_dsse_signer_chain`` /
  ``verify_x509_signature`` accept an optional ``crl_store``; ``None`` = unchanged.
"""

from __future__ import annotations

import datetime
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ObjectIdentifier

from novafabric.trust.novaseal.crl import (
    CrlStatus,
    CrlStore,
    CrlStoreError,
    LoadedCrl,
    check_chain_revocation,
    load_crl_directory,
)
from novafabric.trust.novaseal.x509_identity import (
    X509SigningIdentity,
    validate_certificate_chain,
    verify_dsse_signer_chain,
    verify_x509_signature,
)

from ._x509_pki import (
    NOW,
    Pki,
    Revoked,
    crl_der,
    crl_pem,
    dsse_envelope,
    ecdsa_entry,
    make_cert,
    make_crl,
    make_pki,
)

DAY = datetime.timedelta(days=1)


@pytest.fixture()
def pki() -> Pki:
    return make_pki("crl")


def _chain(pki: Pki) -> list[x509.Certificate]:
    return [pki.leaf.cert, pki.intermediate.cert, pki.root.cert]


def _store(*crls: x509.CertificateRevocationList) -> CrlStore:
    return CrlStore(crls=tuple(LoadedCrl(f"crl{i}.der", c) for i, c in enumerate(crls)))


def _good_store(pki: Pki) -> CrlStore:
    return _store(make_crl(pki.intermediate), make_crl(pki.root))


def _statuses(result: object) -> list[CrlStatus]:
    return [c.status for c in result.certificates]  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Status per certificate
# ---------------------------------------------------------------------------


def test_fresh_crls_mark_whole_path_good(pki: Pki) -> None:
    result = check_chain_revocation(_chain(pki), _good_store(pki))
    assert result.ok
    assert _statuses(result) == [CrlStatus.GOOD, CrlStatus.GOOD]
    assert result.warnings == ()
    assert "good" in result.summary


def test_revoked_leaf_fails_even_in_soft_mode(pki: Pki) -> None:
    when = NOW - DAY / 2
    crl = make_crl(
        pki.intermediate,
        (Revoked(pki.leaf.cert.serial_number, when, x509.ReasonFlags.key_compromise),),
    )
    result = check_chain_revocation(_chain(pki), _store(crl, make_crl(pki.root)))
    assert not result.ok
    leaf = result.certificates[0]
    assert leaf.status is CrlStatus.REVOKED
    assert leaf.reason == "key_compromise"
    assert leaf.revocation_date == when.isoformat()
    assert leaf.serial == format(pki.leaf.cert.serial_number, "x")


def test_revoked_intermediate_fails(pki: Pki) -> None:
    root_crl = make_crl(pki.root, (Revoked(pki.intermediate.cert.serial_number, NOW - DAY),))
    result = check_chain_revocation(_chain(pki), _store(make_crl(pki.intermediate), root_crl))
    assert not result.ok
    assert _statuses(result) == [CrlStatus.GOOD, CrlStatus.REVOKED]
    assert result.certificates[1].reason is None


def test_revocation_after_validation_time_is_not_revoked(pki: Pki) -> None:
    crl = make_crl(pki.intermediate, (Revoked(pki.leaf.cert.serial_number, NOW - DAY / 4),))
    result = check_chain_revocation(
        _chain(pki), _store(crl, make_crl(pki.root)), validation_time=NOW - DAY / 2
    )
    # validation time is before the CRL's thisUpdate (NOW-1h) → stale, never revoked
    assert result.certificates[0].status is CrlStatus.STALE
    ok_at_now = check_chain_revocation(
        _chain(pki),
        _store(
            make_crl(
                pki.intermediate,
                (Revoked(pki.leaf.cert.serial_number, NOW + DAY),),
            ),
            make_crl(pki.root),
        ),
    )
    assert ok_at_now.certificates[0].status is CrlStatus.GOOD


def test_crl_signed_by_wrong_key_is_invalid_and_cannot_revoke(pki: Pki) -> None:
    forged = make_crl(
        pki.intermediate,
        (Revoked(pki.leaf.cert.serial_number, NOW - DAY),),
        signer_key=ec.generate_private_key(ec.SECP256R1()),
    )
    soft = check_chain_revocation(_chain(pki), _store(forged, make_crl(pki.root)))
    assert soft.ok  # soft-fail: forged CRL is a warning, and never a revocation
    assert soft.certificates[0].status is CrlStatus.INVALID_CRL
    assert soft.warnings == (soft.certificates[0],)
    strict = check_chain_revocation(_chain(pki), _store(forged, make_crl(pki.root)), strict=True)
    assert not strict.ok


def test_forged_crl_beside_genuine_one_is_ignored(pki: Pki) -> None:
    forged = make_crl(pki.intermediate, signer_key=ec.generate_private_key(ec.SECP256R1()))
    genuine = make_crl(pki.intermediate, (Revoked(pki.leaf.cert.serial_number, NOW - DAY),))
    result = check_chain_revocation(_chain(pki), _store(forged, genuine, make_crl(pki.root)))
    assert result.certificates[0].status is CrlStatus.REVOKED


def test_stale_crl_warns_soft_and_fails_strict(pki: Pki) -> None:
    stale = make_crl(pki.intermediate, this_update=NOW - 10 * DAY, next_update=NOW - DAY)
    store = _store(stale, make_crl(pki.root))
    soft = check_chain_revocation(_chain(pki), store)
    assert soft.ok
    assert soft.certificates[0].status is CrlStatus.STALE
    assert "not current" in soft.certificates[0].detail
    assert not check_chain_revocation(_chain(pki), store, strict=True).ok


def test_future_crl_is_stale(pki: Pki) -> None:
    future = make_crl(pki.intermediate, this_update=NOW + DAY, next_update=NOW + 2 * DAY)
    result = check_chain_revocation(_chain(pki), _store(future, make_crl(pki.root)))
    assert result.certificates[0].status is CrlStatus.STALE


def test_newest_current_crl_is_reported(pki: Pki) -> None:
    older = make_crl(pki.intermediate, this_update=NOW - 2 * DAY)
    newer = make_crl(pki.intermediate, this_update=NOW - DAY / 24)
    result = check_chain_revocation(_chain(pki), _store(older, newer, make_crl(pki.root)))
    assert result.certificates[0].crl_source == "crl1.der"


def test_revocation_in_stale_crl_still_counts(pki: Pki) -> None:
    stale = make_crl(
        pki.intermediate,
        (Revoked(pki.leaf.cert.serial_number, NOW - 9 * DAY),),
        this_update=NOW - 8 * DAY,
        next_update=NOW - DAY,
    )
    result = check_chain_revocation(_chain(pki), _store(stale, make_crl(pki.root)))
    assert result.certificates[0].status is CrlStatus.REVOKED


def test_certificate_hold_only_counts_from_newest_current_crl(pki: Pki) -> None:
    hold = Revoked(pki.leaf.cert.serial_number, NOW - 9 * DAY, x509.ReasonFlags.certificate_hold)
    stale_hold = make_crl(
        pki.intermediate, (hold,), this_update=NOW - 8 * DAY, next_update=NOW - DAY
    )
    released = make_crl(pki.intermediate)
    result = check_chain_revocation(_chain(pki), _store(stale_hold, released, make_crl(pki.root)))
    assert result.certificates[0].status is CrlStatus.GOOD
    current_hold = make_crl(pki.intermediate, (hold,))
    held = check_chain_revocation(_chain(pki), _store(current_hold, make_crl(pki.root)))
    assert held.certificates[0].status is CrlStatus.REVOKED
    assert held.certificates[0].reason == "certificate_hold"


def test_remove_from_crl_entry_is_ignored(pki: Pki) -> None:
    entry = Revoked(pki.leaf.cert.serial_number, NOW - DAY, x509.ReasonFlags.remove_from_crl)
    crl = make_crl(pki.intermediate, (entry,))
    result = check_chain_revocation(_chain(pki), _store(crl, make_crl(pki.root)))
    assert result.certificates[0].status is CrlStatus.GOOD


def test_missing_crl_soft_and_strict(pki: Pki) -> None:
    store = _store(make_crl(pki.root))  # no CRL from the intermediate
    soft = check_chain_revocation(_chain(pki), store)
    assert soft.ok
    assert _statuses(soft) == [CrlStatus.NO_CRL, CrlStatus.GOOD]
    assert not check_chain_revocation(_chain(pki), store, strict=True).ok


def test_irrelevant_crl_from_other_ca_is_no_crl(pki: Pki) -> None:
    other = make_pki("other")
    store = _store(make_crl(other.intermediate), make_crl(other.root))
    result = check_chain_revocation(_chain(pki), store)
    assert _statuses(result) == [CrlStatus.NO_CRL, CrlStatus.NO_CRL]


def test_issuer_without_crl_sign_key_usage_is_invalid_crl() -> None:
    root = make_cert("ku-root", issuer=None, ca=True)
    inter = make_cert("ku-inter", issuer=root, ca=True, crl_sign=False)
    leaf = make_cert("ku-leaf", issuer=inter, ca=False)
    result = check_chain_revocation(
        [leaf.cert, inter.cert, root.cert], _store(make_crl(inter), make_crl(root))
    )
    assert result.certificates[0].status is CrlStatus.INVALID_CRL
    assert "cRLSign" in result.certificates[0].detail


def test_idp_scope_user_certs_only_does_not_cover_intermediate(pki: Pki) -> None:
    idp = x509.IssuingDistributionPoint(
        full_name=None,
        relative_name=None,
        only_contains_user_certs=True,
        only_contains_ca_certs=False,
        only_some_reasons=None,
        indirect_crl=False,
        only_contains_attribute_certs=False,
    )
    root_crl = make_crl(pki.root, extensions=((idp, True),))
    result = check_chain_revocation(_chain(pki), _store(make_crl(pki.intermediate), root_crl))
    assert result.certificates[1].status is CrlStatus.NO_CRL
    assert "scope mismatch" in result.certificates[1].detail


def test_idp_ca_certs_only_does_not_cover_leaf(pki: Pki) -> None:
    idp = x509.IssuingDistributionPoint(
        full_name=None,
        relative_name=None,
        only_contains_user_certs=False,
        only_contains_ca_certs=True,
        only_some_reasons=None,
        indirect_crl=False,
        only_contains_attribute_certs=False,
    )
    crl = make_crl(pki.intermediate, extensions=((idp, True),))
    result = check_chain_revocation(_chain(pki), _store(crl, make_crl(pki.root)))
    assert result.certificates[0].status is CrlStatus.NO_CRL


def test_idp_full_name_must_match_certificate_cdp() -> None:
    url = "http://crl.example.test/inter.crl"
    root = make_cert("cdp-root", issuer=None, ca=True)
    inter = make_cert("cdp-inter", issuer=root, ca=True)
    leaf = make_cert("cdp-leaf", issuer=inter, ca=False, crl_urls=(url,))
    bare_leaf = make_cert("cdp-bare", issuer=inter, ca=False)

    def idp_crl(name: str) -> x509.CertificateRevocationList:
        idp = x509.IssuingDistributionPoint(
            full_name=[x509.UniformResourceIdentifier(name)],
            relative_name=None,
            only_contains_user_certs=False,
            only_contains_ca_certs=False,
            only_some_reasons=None,
            indirect_crl=False,
            only_contains_attribute_certs=False,
        )
        return make_crl(inter, extensions=((idp, True),))

    matching = check_chain_revocation(
        [leaf.cert, inter.cert, root.cert], _store(idp_crl(url), make_crl(root))
    )
    assert matching.certificates[0].status is CrlStatus.GOOD
    other = check_chain_revocation(
        [leaf.cert, inter.cert, root.cert],
        _store(idp_crl("http://elsewhere.test/x.crl"), make_crl(root)),
    )
    assert other.certificates[0].status is CrlStatus.NO_CRL
    no_cdp = check_chain_revocation(
        [bare_leaf.cert, inter.cert, root.cert], _store(idp_crl(url), make_crl(root))
    )
    assert no_cdp.certificates[0].status is CrlStatus.NO_CRL


def test_naive_validation_time_and_anchor_only_chain(pki: Pki) -> None:
    naive = NOW.replace(tzinfo=None)
    result = check_chain_revocation(_chain(pki), _good_store(pki), validation_time=naive)
    assert result.ok
    empty = check_chain_revocation([pki.root.cert], _good_store(pki))
    assert empty.ok and empty.certificates == ()
    assert "no certificate" in empty.summary


# ---------------------------------------------------------------------------
# Directory loading (bounded, offline)
# ---------------------------------------------------------------------------


def test_load_directory_der_and_pem_and_findings(tmp_path: Path, pki: Pki) -> None:
    (tmp_path / "a-inter.crl").write_bytes(crl_der(make_crl(pki.intermediate)))
    (tmp_path / "b-root.pem").write_bytes(crl_pem(make_crl(pki.root)))
    (tmp_path / "c-readme.txt").write_text("not a crl")
    (tmp_path / "d-cert.pem").write_bytes(pki.leaf.cert_pem)
    (tmp_path / ".partial").write_bytes(b"ignored")
    (tmp_path / "subdir").mkdir()
    store = load_crl_directory(tmp_path)
    assert [c.source for c in store.crls] == ["a-inter.crl", "b-root.pem"]
    assert [f.source for f in store.findings] == ["c-readme.txt", "d-cert.pem"]
    assert all("not a DER or PEM" in f.message for f in store.findings)
    assert check_chain_revocation(_chain(pki), store).ok


def test_oversize_file_is_skipped_with_finding(tmp_path: Path, pki: Pki) -> None:
    data = crl_der(make_crl(pki.intermediate))
    (tmp_path / "big.crl").write_bytes(data)
    store = load_crl_directory(tmp_path, max_bytes=len(data) - 1)
    assert store.crls == ()
    assert "larger than" in store.findings[0].message
    assert len(load_crl_directory(tmp_path, max_bytes=len(data)).crls) == 1


def test_too_many_files_fails_closed(tmp_path: Path) -> None:
    for i in range(3):
        (tmp_path / f"{i}.crl").write_bytes(b"x")
    with pytest.raises(CrlStoreError, match="over the limit"):
        load_crl_directory(tmp_path, max_files=2)


def test_missing_directory_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(CrlStoreError, match="cannot read CRL directory"):
        load_crl_directory(tmp_path / "absent")


def test_bad_limits_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="positive"):
        load_crl_directory(tmp_path, max_files=0)


def test_unreadable_file_is_a_finding(
    tmp_path: Path, pki: Pki, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "x.crl").write_bytes(crl_der(make_crl(pki.intermediate)))
    real_open = Path.open

    def boom(self: Path, *args: object, **kwargs: object) -> object:
        if self.name == "x.crl":
            raise PermissionError("denied")
        return real_open(self, *args, **kwargs)  # type: ignore[call-overload]

    monkeypatch.setattr(Path, "open", boom)
    store = load_crl_directory(tmp_path)
    assert store.crls == () and "unreadable" in store.findings[0].message


def _idp(**kw: object) -> x509.IssuingDistributionPoint:
    args: dict[str, object] = {
        "full_name": None,
        "relative_name": None,
        "only_contains_user_certs": False,
        "only_contains_ca_certs": False,
        "only_some_reasons": None,
        "indirect_crl": False,
        "only_contains_attribute_certs": False,
    }
    args.update(kw)
    return x509.IssuingDistributionPoint(**args)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("extension", "critical", "message"),
    [
        (x509.DeltaCRLIndicator(1), True, "delta CRL"),
        (_idp(indirect_crl=True), True, "indirect CRL"),
        (_idp(only_contains_attribute_certs=True), True, "attribute-certificate"),
        (
            _idp(only_some_reasons=frozenset({x509.ReasonFlags.key_compromise})),
            True,
            "reason-partitioned",
        ),
        (
            _idp(
                relative_name=x509.RelativeDistinguishedName(
                    [x509.NameAttribute(x509.oid.NameOID.COMMON_NAME, "dp")]
                )
            ),
            True,
            "relative distribution-point",
        ),
        (
            x509.UnrecognizedExtension(ObjectIdentifier("1.3.6.1.4.1.99999.1"), b"\x05\x00"),
            True,
            "unsupported critical CRL extension",
        ),
    ],
)
def test_unsupported_crl_kinds_are_skipped(
    tmp_path: Path, pki: Pki, extension: x509.ExtensionType, critical: bool, message: str
) -> None:
    crl = make_crl(pki.intermediate, extensions=((extension, critical),))
    (tmp_path / "x.crl").write_bytes(crl_der(crl))
    store = load_crl_directory(tmp_path)
    assert store.crls == ()
    assert message in store.findings[0].message


def test_non_critical_unknown_extension_is_accepted(tmp_path: Path, pki: Pki) -> None:
    ext = x509.UnrecognizedExtension(ObjectIdentifier("1.3.6.1.4.1.99999.2"), b"\x05\x00")
    (tmp_path / "x.crl").write_bytes(
        crl_der(make_crl(pki.intermediate, extensions=((ext, False),)))
    )
    assert len(load_crl_directory(tmp_path).crls) == 1


# ---------------------------------------------------------------------------
# Wiring into chain validation (additive, default None = unchanged)
# ---------------------------------------------------------------------------


def _anchors(pki: Pki) -> list[x509.Certificate]:
    return [pki.root.cert, pki.intermediate.cert]


def test_validate_chain_without_store_is_unchanged(pki: Pki) -> None:
    result = validate_certificate_chain(pki.leaf.cert, _anchors(pki))
    assert result.valid and result.revocation is None


def test_validate_chain_with_store_checks_revocation(pki: Pki) -> None:
    # anchors = root only; intermediate supplied as untrusted → both checked
    ok = validate_certificate_chain(
        pki.leaf.cert,
        [pki.root.cert],
        intermediates=[pki.intermediate.cert],
        crl_store=_good_store(pki),
    )
    assert ok.valid and ok.revocation is not None
    assert _statuses(ok.revocation) == [CrlStatus.GOOD, CrlStatus.GOOD]
    revoked_store = _store(
        make_crl(pki.intermediate, (Revoked(pki.leaf.cert.serial_number, NOW - DAY),)),
        make_crl(pki.root),
    )
    bad = validate_certificate_chain(
        pki.leaf.cert,
        [pki.root.cert],
        intermediates=[pki.intermediate.cert],
        crl_store=revoked_store,
    )
    assert not bad.valid
    assert "revocation check failed" in bad.reason
    assert bad.chain_subjects  # the path itself validated


def test_validate_chain_strict_missing_crl_fails(pki: Pki) -> None:
    empty = CrlStore(crls=())
    soft = validate_certificate_chain(pki.leaf.cert, _anchors(pki), crl_store=empty)
    assert soft.valid and soft.revocation is not None and soft.revocation.warnings
    strict = validate_certificate_chain(
        pki.leaf.cert, _anchors(pki), crl_store=empty, crl_strict=True
    )
    assert not strict.valid


def test_dsse_signer_chain_with_revoked_leaf_fails(pki: Pki) -> None:
    payload = b'{"run_id": "crl"}'
    env = dsse_envelope(payload, [ecdsa_entry(pki.leaf, payload)])
    good = verify_dsse_signer_chain(env, _anchors(pki), crl_store=_good_store(pki))
    assert good.valid and good.revocation is not None
    revoked_store = _store(
        make_crl(pki.intermediate, (Revoked(pki.leaf.cert.serial_number, NOW - DAY),)),
    )
    bad = verify_dsse_signer_chain(env, _anchors(pki), crl_store=revoked_store)
    assert not bad.valid
    assert "revocation check failed" in bad.reason


def test_verify_x509_signature_with_revoked_leaf_fails(pki: Pki) -> None:
    identity = X509SigningIdentity.from_pem(pki.leaf.key_pem, pki.leaf.cert_pem)
    sig = identity.sign(b"payload")
    ok = verify_x509_signature(b"payload", sig, ca_bundle=_anchors(pki), crl_store=_good_store(pki))
    assert ok.valid
    revoked_store = _store(
        make_crl(pki.intermediate, (Revoked(pki.leaf.cert.serial_number, NOW - DAY),)),
    )
    bad = verify_x509_signature(b"payload", sig, ca_bundle=_anchors(pki), crl_store=revoked_store)
    assert not bad.valid
    assert "revocation" in bad.reason


def test_bundled_intermediate_anchor_is_still_revocation_checked(pki: Pki) -> None:
    """root+intermediate in the bundle: path ends at the intermediate, root CRL still applies."""
    root_crl = make_crl(pki.root, (Revoked(pki.intermediate.cert.serial_number, NOW - DAY),))
    result = validate_certificate_chain(
        pki.leaf.cert, _anchors(pki), crl_store=_store(make_crl(pki.intermediate), root_crl)
    )
    assert not result.valid
    assert result.revocation is not None
    assert _statuses(result.revocation) == [CrlStatus.GOOD, CrlStatus.REVOKED]


def test_intermediate_anchor_without_its_issuer_is_not_extended(pki: Pki) -> None:
    result = validate_certificate_chain(
        pki.leaf.cert, [pki.intermediate.cert], crl_store=_good_store(pki)
    )
    assert result.valid and result.revocation is not None
    assert len(result.revocation.certificates) == 1


def test_same_name_impostor_issuer_is_not_used_to_extend(pki: Pki) -> None:
    impostor = make_cert("crl-root-ca", issuer=None, ca=True)  # same subject, other key
    result = validate_certificate_chain(
        pki.leaf.cert, [pki.intermediate.cert, impostor.cert], crl_store=_good_store(pki)
    )
    assert result.valid and result.revocation is not None
    assert len(result.revocation.certificates) == 1


def test_extensionless_certificates_use_rfc5280_defaults() -> None:
    """No keyUsage → issuer may sign CRLs; no basicConstraints → treated as end entity."""
    from cryptography.hazmat.primitives import hashes

    from ._x509_pki import Node

    def bare(cn: str, issuer: Node | None) -> Node:
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(x509.oid.NameOID.COMMON_NAME, cn)])
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(issuer.cert.subject if issuer else name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(NOW - DAY)
            .not_valid_after(NOW + DAY)
            .sign(issuer.key if issuer else key, hashes.SHA256())
        )
        return Node(cert=cert, key=key)

    issuer = bare("bare-issuer", None)
    leaf = bare("bare-leaf", issuer)
    crl = make_crl(issuer, extensions=((_idp(only_contains_user_certs=True), True),))
    result = check_chain_revocation([leaf.cert, issuer.cert], _store(crl))
    assert result.certificates[0].status is CrlStatus.GOOD


# ---------------------------------------------------------------------------
# Retroactive revocation and point-in-time (freshness_time) checks
# ---------------------------------------------------------------------------


def _crl_with_invalidity(
    pki: Pki, revoked_at: datetime.datetime, invalid_from: datetime.datetime
) -> x509.CertificateRevocationList:
    from cryptography.hazmat.primitives import hashes  # noqa: PLC0415

    entry = (
        x509.RevokedCertificateBuilder()
        .serial_number(pki.leaf.cert.serial_number)
        .revocation_date(revoked_at)
        .add_extension(x509.InvalidityDate(invalid_from), critical=False)
        .build()
    )
    return (
        x509.CertificateRevocationListBuilder()
        .issuer_name(pki.intermediate.cert.subject)
        .last_update(NOW - DAY / 24)
        .next_update(NOW + 7 * DAY)
        .add_revoked_certificate(entry)
        .sign(pki.intermediate.key, hashes.SHA256())
    )


def _as_of(pki: Pki, crl: x509.CertificateRevocationList, *, strict: bool = False) -> object:
    """Evaluate as of two days ago, with CRL freshness judged now (TSA semantics)."""
    store = CrlStore(
        crls=(LoadedCrl("leaf.der", crl), LoadedCrl("root.der", make_crl(pki.root))),
        freshness_time=NOW,
    )
    return check_chain_revocation(_chain(pki), store, validation_time=NOW - 2 * DAY, strict=strict)


@pytest.mark.parametrize(
    "reason", [x509.ReasonFlags.key_compromise, x509.ReasonFlags.ca_compromise]
)
def test_compromise_revocation_is_retroactive(pki: Pki, reason: x509.ReasonFlags) -> None:
    crl = make_crl(pki.intermediate, (Revoked(pki.leaf.cert.serial_number, NOW - DAY, reason),))
    result = _as_of(pki, crl)
    assert not result.ok  # type: ignore[attr-defined]
    leaf = result.certificates[0]  # type: ignore[attr-defined]
    assert leaf.status is CrlStatus.REVOKED and leaf.reason == reason.name
    assert "retroactive" in leaf.detail


@pytest.mark.parametrize(
    "reason",
    [None, x509.ReasonFlags.superseded, x509.ReasonFlags.cessation_of_operation],
)
def test_other_reasons_after_validation_time_do_not_revoke(
    pki: Pki, reason: x509.ReasonFlags | None
) -> None:
    crl = make_crl(pki.intermediate, (Revoked(pki.leaf.cert.serial_number, NOW - DAY, reason),))
    result = _as_of(pki, crl, strict=True)
    assert result.ok, result.summary  # type: ignore[attr-defined]
    assert _statuses(result) == [CrlStatus.GOOD, CrlStatus.GOOD]


def test_invalidity_date_before_validation_time_revokes(pki: Pki) -> None:
    early = _as_of(pki, _crl_with_invalidity(pki, NOW - DAY, NOW - 3 * DAY))
    assert _statuses(early)[0] is CrlStatus.REVOKED
    assert "invalidityDate" in early.certificates[0].detail  # type: ignore[attr-defined]
    late = _as_of(pki, _crl_with_invalidity(pki, NOW - DAY, NOW - DAY))
    assert _statuses(late)[0] is CrlStatus.GOOD


def test_freshness_time_judges_currency_now_not_at_validation_time(pki: Pki) -> None:
    # CRLs issued an hour ago: stale at a validation time two days back ...
    without = check_chain_revocation(
        _chain(pki), _good_store(pki), validation_time=NOW - 2 * DAY, strict=True
    )
    assert not without.ok and _statuses(without) == [CrlStatus.STALE, CrlStatus.STALE]
    assert "validation time" in without.certificates[0].detail
    # ... but current when freshness is judged at the verification instant.
    fresh = CrlStore(crls=_good_store(pki).crls, freshness_time=NOW)
    with_fresh = check_chain_revocation(
        _chain(pki), fresh, validation_time=NOW - 2 * DAY, strict=True
    )
    assert with_fresh.ok and _statuses(with_fresh) == [CrlStatus.GOOD, CrlStatus.GOOD]


def test_freshness_time_still_flags_a_crl_stale_now(pki: Pki) -> None:
    old = make_crl(pki.intermediate, this_update=NOW - 10 * DAY, next_update=NOW - DAY)
    store = CrlStore(
        crls=(LoadedCrl("old.der", old), LoadedCrl("root.der", make_crl(pki.root))),
        freshness_time=NOW,
    )
    result = check_chain_revocation(_chain(pki), store, validation_time=NOW - 5 * DAY, strict=True)
    assert not result.ok
    assert result.certificates[0].status is CrlStatus.STALE
    assert "verification time" in result.certificates[0].detail
