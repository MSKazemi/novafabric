"""Coverage tests for crypto/trust modules — RFC 3161 chain verification,
Sigstore signing/verify (mocked SDK), and the Postgres Merkle log (mocked psycopg).

All external dependencies are mocked: no real TSA / OCSP / CRL network calls,
no real Sigstore OIDC/Rekor, no real Postgres connection.  The SQLite ``MerkleLog``
branches are exercised against real in-memory/temp SQLite (deterministic).

Mirrors the established mocking style of:
    tests/seal/test_rfc3161_hardening.py   (TSA httpx.post mocking, DER builders)
    tests/seal/test_timestamp.py           (real cert builders via cryptography)
    tests/seal/test_sigstore_signer.py     (sys.modules ImportError injection)
    tests/seal/test_postgres_merkle.py     (PostgresMerkleLog lazy-connect)
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import cryptography.x509 as cx509
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.hazmat.primitives.serialization import Encoding
from cryptography.x509.oid import NameOID

from novafabric.trust import _rfc3161
from novafabric.trust._rfc3161 import (
    TsaChainResult,
    _build_cert_chain,
    _check_crl_reachability,
    _check_ocsp,
    _der_integer,
    _extract_signing_cert_from_tsr,
    _get_crl_urls,
    _get_ocsp_url,
    _parse_pem_bundle,
    verify_tsa_chain,
)

# ===========================================================================
# Shared cert builders (real cryptography objects, no network)
# ===========================================================================


def _self_signed_cert(
    *,
    common_name: str = "test-tsa",
    ocsp_url: str | None = None,
    crl_url: str | None = None,
    key: ec.EllipticCurvePrivateKey | None = None,
) -> tuple[x509.Certificate, ec.EllipticCurvePrivateKey]:
    """Build a self-signed ECDSA P-256 cert, optionally with AIA/CRL extensions."""
    key = key or ec.generate_private_key(ec.SECP256R1())
    name = cx509.Name([cx509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    builder = (
        cx509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(cx509.random_serial_number())
        .not_valid_before(datetime(2020, 1, 1, tzinfo=timezone.utc))
        .not_valid_after(datetime(2030, 1, 1, tzinfo=timezone.utc))
    )
    if ocsp_url is not None:
        builder = builder.add_extension(
            x509.AuthorityInformationAccess(
                [
                    x509.AccessDescription(
                        x509.oid.AuthorityInformationAccessOID.OCSP,
                        x509.UniformResourceIdentifier(ocsp_url),
                    )
                ]
            ),
            critical=False,
        )
    if crl_url is not None:
        builder = builder.add_extension(
            x509.CRLDistributionPoints(
                [
                    x509.DistributionPoint(
                        full_name=[x509.UniformResourceIdentifier(crl_url)],
                        relative_name=None,
                        reasons=None,
                        crl_issuer=None,
                    )
                ]
            ),
            critical=False,
        )
    cert = builder.sign(key, hashes.SHA256())
    return cert, key


def _ca_signed_cert(
    issuer_cert: x509.Certificate,
    issuer_key: ec.EllipticCurvePrivateKey | rsa.RSAPrivateKey,
    *,
    leaf_key: ec.EllipticCurvePrivateKey | None = None,
) -> x509.Certificate:
    """Build a leaf cert signed by *issuer_key* (so issuer != subject)."""
    leaf_key = leaf_key or ec.generate_private_key(ec.SECP256R1())
    subject = cx509.Name([cx509.NameAttribute(NameOID.COMMON_NAME, "leaf-tsa")])
    return (
        cx509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer_cert.subject)
        .public_key(leaf_key.public_key())
        .serial_number(cx509.random_serial_number())
        .not_valid_before(datetime(2020, 1, 1, tzinfo=timezone.utc))
        .not_valid_after(datetime(2030, 1, 1, tzinfo=timezone.utc))
        .sign(issuer_key, hashes.SHA256())
    )


def _tsr_embedding_cert(cert: x509.Certificate) -> bytes:
    """Build TSR-shaped bytes that embed a real cert so the brute-force scanner
    in ``_extract_signing_cert_from_tsr`` locates it (cert length 200-8192 B,
    SEQUENCE tag 0x30, parseable DER)."""
    cert_der = cert.public_bytes(Encoding.DER)
    # Prepend a small PKIStatusInfo-ish blob, then the cert DER verbatim.
    return b"\x30\x03\x02\x01\x00" + cert_der


# ===========================================================================
# _extract_signing_cert_from_tsr
# ===========================================================================


class TestExtractSigningCert:
    def test_extracts_real_cert_from_tsr(self):
        cert, _ = _self_signed_cert()
        tsr = _tsr_embedding_cert(cert)
        extracted = _extract_signing_cert_from_tsr(tsr)
        assert extracted is not None
        assert extracted.subject == cert.subject

    def test_returns_none_when_no_cert(self):
        # Short buffer with no embedded cert-sized SEQUENCE.
        assert _extract_signing_cert_from_tsr(b"\x30\x03\x02\x01\x00") is None

    def test_returns_none_on_garbage(self):
        assert _extract_signing_cert_from_tsr(b"\xff" * 50) is None

    def test_import_absent_returns_none(self, monkeypatch):
        """When cryptography.x509 import fails inside the function → None (line 498-499).

        Build the cert/TSR *before* poisoning sys.modules — cert creation itself
        imports cryptography.x509, so poisoning first made the test order-dependent
        (it only passed when a sibling test had already warmed the import).
        """
        cert, _ = _self_signed_cert()
        tsr = _tsr_embedding_cert(cert)
        monkeypatch.setitem(sys.modules, "cryptography.x509", None)
        assert _extract_signing_cert_from_tsr(tsr) is None


# ===========================================================================
# _get_ocsp_url / _get_crl_urls
# ===========================================================================


class TestExtractUrls:
    def test_get_ocsp_url_present(self):
        cert, _ = _self_signed_cert(ocsp_url="http://ocsp.example.com")
        assert _get_ocsp_url(cert) == "http://ocsp.example.com"

    def test_get_ocsp_url_absent_returns_none(self):
        cert, _ = _self_signed_cert()  # no AIA extension
        assert _get_ocsp_url(cert) is None

    def test_get_ocsp_url_bad_input_returns_none(self):
        assert _get_ocsp_url(object()) is None

    def test_get_crl_urls_present(self):
        cert, _ = _self_signed_cert(crl_url="http://crl.example.com/a.crl")
        urls = _get_crl_urls(cert)
        assert "http://crl.example.com/a.crl" in urls

    def test_get_crl_urls_absent_returns_empty(self):
        cert, _ = _self_signed_cert()  # no CDP extension
        assert _get_crl_urls(cert) == []

    def test_get_crl_urls_bad_input_returns_empty(self):
        assert _get_crl_urls(object()) == []


# ===========================================================================
# _check_ocsp — mock urllib.request.urlopen (no network)
# ===========================================================================


class TestCheckOcsp:
    def test_no_ocsp_url_returns_unknown(self):
        cert, _ = _self_signed_cert()  # no AIA → no OCSP url
        errors: list[str] = []
        assert _check_ocsp(cert, errors) == _rfc3161._REVOCATION_UNKNOWN

    def test_reachable_responder_returns_unknown_with_note(self):
        cert, _ = _self_signed_cert(ocsp_url="http://ocsp.example.com")
        errors: list[str] = []
        mock_resp = MagicMock()
        mock_resp.getcode.return_value = 200
        mock_resp.__enter__.return_value = mock_resp
        mock_resp.__exit__.return_value = False
        with patch("novafabric.trust._rfc3161.urllib.request.urlopen", return_value=mock_resp):
            result = _check_ocsp(cert, errors)
        assert result == _rfc3161._REVOCATION_UNKNOWN
        assert any("OCSP responder reachable" in e for e in errors)

    def test_network_failure_returns_unknown_with_note(self):
        cert, _ = _self_signed_cert(ocsp_url="http://ocsp.example.com")
        errors: list[str] = []
        with patch(
            "novafabric.trust._rfc3161.urllib.request.urlopen",
            side_effect=OSError("timeout"),
        ):
            result = _check_ocsp(cert, errors)
        assert result == _rfc3161._REVOCATION_UNKNOWN
        assert any("OCSP HEAD request failed" in e for e in errors)


# ===========================================================================
# _check_crl_reachability — mock urlopen
# ===========================================================================


class TestCheckCrlReachability:
    def test_no_crl_urls_returns_unknown(self):
        cert, _ = _self_signed_cert()
        errors: list[str] = []
        assert _check_crl_reachability(cert, errors) == _rfc3161._REVOCATION_UNKNOWN

    def test_reachable_crl_returns_unknown_with_note(self):
        cert, _ = _self_signed_cert(crl_url="http://crl.example.com/a.crl")
        errors: list[str] = []
        mock_resp = MagicMock()
        mock_resp.getcode.return_value = 200
        mock_resp.__enter__.return_value = mock_resp
        mock_resp.__exit__.return_value = False
        with patch("novafabric.trust._rfc3161.urllib.request.urlopen", return_value=mock_resp):
            result = _check_crl_reachability(cert, errors)
        assert result == _rfc3161._REVOCATION_UNKNOWN
        assert any("CRL distribution point reachable" in e for e in errors)

    def test_non_http_url_skipped(self):
        cert, _ = _self_signed_cert(crl_url="ldap://crl.example.com/a.crl")
        errors: list[str] = []
        # ldap:// is skipped; loop completes with no urlopen call.
        with patch(
            "novafabric.trust._rfc3161.urllib.request.urlopen",
            side_effect=AssertionError("urlopen should not be called for ldap://"),
        ):
            assert _check_crl_reachability(cert, errors) == _rfc3161._REVOCATION_UNKNOWN

    def test_crl_unreachable_records_error(self):
        cert, _ = _self_signed_cert(crl_url="http://crl.example.com/a.crl")
        errors: list[str] = []
        with patch(
            "novafabric.trust._rfc3161.urllib.request.urlopen",
            side_effect=OSError("conn refused"),
        ):
            result = _check_crl_reachability(cert, errors)
        assert result == _rfc3161._REVOCATION_UNKNOWN
        assert any("CRL HEAD unreachable" in e for e in errors)


# ===========================================================================
# _build_cert_chain
# ===========================================================================


class TestBuildCertChain:
    def test_self_signed_no_trusted_ca_accepts_degraded(self):
        cert, _ = _self_signed_cert()
        ok, errors = _build_cert_chain(cert, None)
        assert ok is True
        assert any("Self-signed cert accepted" in e for e in errors)

    def test_self_signed_in_trusted_bundle_passes(self):
        cert, _ = _self_signed_cert()
        pem = cert.public_bytes(Encoding.PEM).decode()
        ok, errors = _build_cert_chain(cert, pem)
        assert ok is True
        assert errors == []

    def test_self_signed_not_in_trusted_bundle_fails(self):
        cert, _ = _self_signed_cert()
        other, _ = _self_signed_cert(common_name="someone-else")
        other_pem = other.public_bytes(Encoding.PEM).decode()
        ok, errors = _build_cert_chain(cert, other_pem)
        assert ok is False
        assert any("not found in provided trusted_ca_pem" in e for e in errors)

    def test_ca_signed_no_trusted_ca_degraded(self):
        ca_cert, ca_key = _self_signed_cert(common_name="ca")
        leaf = _ca_signed_cert(ca_cert, ca_key)
        ok, errors = _build_cert_chain(leaf, None)
        assert ok is True
        assert any("without chain verification" in e for e in errors)

    def test_ca_signed_issuer_in_bundle_ecdsa_verifies(self):
        ca_cert, ca_key = _self_signed_cert(common_name="ca-ec")
        leaf = _ca_signed_cert(ca_cert, ca_key)
        ca_pem = ca_cert.public_bytes(Encoding.PEM).decode()
        ok, errors = _build_cert_chain(leaf, ca_pem)
        assert ok is True
        assert errors == []

    def test_ca_signed_issuer_in_bundle_rsa_verifies(self):
        ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = cx509.Name([cx509.NameAttribute(NameOID.COMMON_NAME, "ca-rsa")])
        ca_cert = (
            cx509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(ca_key.public_key())
            .serial_number(cx509.random_serial_number())
            .not_valid_before(datetime(2020, 1, 1, tzinfo=timezone.utc))
            .not_valid_after(datetime(2030, 1, 1, tzinfo=timezone.utc))
            .sign(ca_key, hashes.SHA256())
        )
        leaf = _ca_signed_cert(ca_cert, ca_key)
        ca_pem = ca_cert.public_bytes(Encoding.PEM).decode()
        ok, errors = _build_cert_chain(leaf, ca_pem)
        assert ok is True
        assert errors == []

    def test_ca_signed_issuer_not_in_bundle_fails(self):
        ca_cert, ca_key = _self_signed_cert(common_name="real-ca")
        leaf = _ca_signed_cert(ca_cert, ca_key)
        unrelated, _ = _self_signed_cert(common_name="unrelated-ca")
        ok, errors = _build_cert_chain(leaf, unrelated.public_bytes(Encoding.PEM).decode())
        assert ok is False
        assert any("Issuer cert not found" in e for e in errors)

    def test_ca_signed_wrong_issuer_signature_fails(self):
        """Issuer subject matches but signature was made by a different key →
        signature verification failure branch."""
        ca_cert, ca_key = _self_signed_cert(common_name="impostor-ca")
        # Sign the leaf with a *different* key than ca_cert's public key.
        rogue_key = ec.generate_private_key(ec.SECP256R1())
        leaf = (
            cx509.CertificateBuilder()
            .subject_name(cx509.Name([cx509.NameAttribute(NameOID.COMMON_NAME, "victim")]))
            .issuer_name(ca_cert.subject)
            .public_key(ec.generate_private_key(ec.SECP256R1()).public_key())
            .serial_number(cx509.random_serial_number())
            .not_valid_before(datetime(2020, 1, 1, tzinfo=timezone.utc))
            .not_valid_after(datetime(2030, 1, 1, tzinfo=timezone.utc))
            .sign(rogue_key, hashes.SHA256())
        )
        ca_pem = ca_cert.public_bytes(Encoding.PEM).decode()
        ok, errors = _build_cert_chain(leaf, ca_pem)
        assert ok is False
        assert any("signature verification failed" in e.lower() for e in errors)

    def test_bad_cert_object_returns_error(self):
        ok, errors = _build_cert_chain(object(), None)
        assert ok is False
        assert errors  # assertion error captured in the generic except


# ===========================================================================
# _parse_pem_bundle
# ===========================================================================


class TestParsePemBundle:
    def test_parses_single_cert(self):
        cert, _ = _self_signed_cert()
        pem = cert.public_bytes(Encoding.PEM).decode()
        certs = _parse_pem_bundle(pem)
        assert len(certs) == 1
        assert certs[0].subject == cert.subject

    def test_parses_multiple_certs(self):
        c1, _ = _self_signed_cert(common_name="one")
        c2, _ = _self_signed_cert(common_name="two")
        pem = (
            c1.public_bytes(Encoding.PEM).decode()
            + c2.public_bytes(Encoding.PEM).decode()
        )
        certs = _parse_pem_bundle(pem)
        assert len(certs) == 2

    def test_empty_pem_returns_empty(self):
        assert _parse_pem_bundle("") == []

    def test_garbage_between_markers_skipped(self):
        garbage = "-----BEGIN CERTIFICATE-----\nNOTBASE64\n-----END CERTIFICATE-----\n"
        assert _parse_pem_bundle(garbage) == []


# ===========================================================================
# verify_tsa_chain — top-level orchestration
# ===========================================================================


class TestVerifyTsaChain:
    def test_no_cert_in_tsr_returns_chain_not_ok(self):
        result = verify_tsa_chain(b"\x30\x03\x02\x01\x00")
        assert isinstance(result, TsaChainResult)
        assert result.chain_ok is False
        assert result.revocation_status == _rfc3161._REVOCATION_UNKNOWN
        assert any("Could not extract signing certificate" in e for e in result.errors)

    def test_self_signed_cert_with_explicit_pem(self):
        cert, _ = _self_signed_cert()
        tsr = _tsr_embedding_cert(cert)
        pem = cert.public_bytes(Encoding.PEM).decode()
        result = verify_tsa_chain(tsr, trusted_ca_pem=pem)
        assert result.chain_ok is True

    def test_ocsp_branch_taken_when_responder_reachable(self):
        """When OCSP returns a definitive status (mock returns 'good'), the OCSP
        branch sets ocsp_checked=True (lines 768-770)."""
        cert, _ = _self_signed_cert(ocsp_url="http://ocsp.example.com")
        tsr = _tsr_embedding_cert(cert)
        pem = cert.public_bytes(Encoding.PEM).decode()
        with patch(
            "novafabric.trust._rfc3161._check_ocsp",
            return_value=_rfc3161._REVOCATION_GOOD,
        ):
            result = verify_tsa_chain(tsr, trusted_ca_pem=pem)
        assert result.ocsp_checked is True
        assert result.revocation_status == _rfc3161._REVOCATION_GOOD

    def test_falls_back_to_crl_when_ocsp_unknown(self):
        cert, _ = _self_signed_cert()
        tsr = _tsr_embedding_cert(cert)
        pem = cert.public_bytes(Encoding.PEM).decode()
        with patch(
            "novafabric.trust._rfc3161._check_crl_reachability",
            return_value=_rfc3161._REVOCATION_REVOKED,
        ) as mock_crl:
            result = verify_tsa_chain(tsr, trusted_ca_pem=pem)
        mock_crl.assert_called_once()
        assert result.ocsp_checked is False
        assert result.revocation_status == _rfc3161._REVOCATION_REVOKED

    def test_default_system_ca_path_read(self, monkeypatch):
        """trusted_ca_pem=None → reads system CA bundle path (lines 732-743)."""
        cert, _ = _self_signed_cert()
        tsr = _tsr_embedding_cert(cert)
        # Force a deterministic verify-paths result pointing at a temp PEM.
        monkeypatch.setattr(
            _rfc3161.ssl,
            "get_default_verify_paths",
            lambda: SimpleNamespace(cafile=None, openssl_cafile=None),
        )
        result = verify_tsa_chain(tsr)  # no trusted_ca_pem
        assert isinstance(result, TsaChainResult)

    def test_system_ca_read_error_recorded(self, monkeypatch, tmp_path):
        cert, _ = _self_signed_cert()
        tsr = _tsr_embedding_cert(cert)
        missing = tmp_path / "does-not-exist.pem"
        monkeypatch.setattr(
            _rfc3161.ssl,
            "get_default_verify_paths",
            lambda: SimpleNamespace(cafile=str(missing), openssl_cafile=None),
        )
        result = verify_tsa_chain(tsr)
        assert any("Could not read system CA bundle" in e for e in result.errors)

    def test_get_default_verify_paths_raises_recorded(self, monkeypatch):
        cert, _ = _self_signed_cert()
        tsr = _tsr_embedding_cert(cert)

        def _boom():
            raise RuntimeError("no ssl")

        monkeypatch.setattr(_rfc3161.ssl, "get_default_verify_paths", _boom)
        result = verify_tsa_chain(tsr)
        assert any("get_default_verify_paths() failed" in e for e in result.errors)


# ===========================================================================
# DER integer edge branches (lines 136, 139)
# ===========================================================================


class TestDerIntegerEdges:
    def test_high_bit_set_prepends_zero(self):
        # 0x80 has high bit set → expect leading 0x00 (line 139).
        encoded = _der_integer(0x80)
        assert encoded == bytes([0x02, 0x02, 0x00, 0x80])

    def test_leading_zero_trimmed(self):
        # 0x0100 → 2 bytes 0x01 0x00; no spurious leading zero (line 135-136).
        encoded = _der_integer(0x0100)
        assert encoded == bytes([0x02, 0x02, 0x01, 0x00])


# ===========================================================================
# Sigstore signer — mock the SDK objects (sigstore 4.x installed)
# ===========================================================================

from novafabric.trust.novaseal.sigstore_signer import (  # noqa: E402
    SigstoreSigner,
    _extract_identity,
    _extract_rekor_log_index,
)


def _install_fake_sigstore_sign(monkeypatch, *, bundle_json='{"ok": true}', ambient=True):
    """Inject fake sigstore.sign / sigstore.oidc / sigstore.models modules so
    SigstoreSigner.sign_artifact runs the full happy path without network."""
    fake_bundle = MagicMock()
    fake_bundle.to_json.return_value = bundle_json

    fake_signer = MagicMock()
    fake_signer.sign_artifact.return_value = fake_bundle
    fake_signer.__enter__.return_value = fake_signer
    fake_signer.__exit__.return_value = False

    fake_ctx = MagicMock()
    fake_ctx.signer.return_value = fake_signer

    sign_mod = SimpleNamespace(
        SigningContext=SimpleNamespace(from_trust_config=lambda tc: fake_ctx)
    )

    trust_config = MagicMock()
    trust_config.signing_config.get_oidc_url.return_value = "https://oauth.example"
    models_mod = SimpleNamespace(
        ClientTrustConfig=SimpleNamespace(production=lambda: trust_config),
        Bundle=MagicMock(),
    )

    raw_token = "raw-token" if ambient else None
    issuer_obj = MagicMock()
    issuer_obj.identity_token.return_value = MagicMock()
    oidc_mod = SimpleNamespace(
        IdentityToken=lambda t: MagicMock(),
        Issuer=lambda url: issuer_obj,
        detect_credential=lambda: raw_token,
    )

    monkeypatch.setitem(sys.modules, "sigstore", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "sigstore.sign", sign_mod)
    monkeypatch.setitem(sys.modules, "sigstore.oidc", oidc_mod)
    monkeypatch.setitem(sys.modules, "sigstore.models", models_mod)
    return fake_signer, issuer_obj


class TestSigstoreSignArtifact:
    def test_sign_happy_path_ambient_credential(self, monkeypatch):
        fake_signer, _ = _install_fake_sigstore_sign(monkeypatch, bundle_json='{"x": 1}')
        signer = SigstoreSigner()
        result = signer.sign_artifact(b"artifact-bytes")
        assert result == {"x": 1}
        fake_signer.sign_artifact.assert_called_once_with(b"artifact-bytes")

    def test_sign_interactive_issuer_when_no_ambient(self, monkeypatch):
        _, issuer_obj = _install_fake_sigstore_sign(
            monkeypatch, bundle_json='{"y": 2}', ambient=False
        )
        signer = SigstoreSigner()
        result = signer.sign_artifact(b"a")
        assert result == {"y": 2}
        issuer_obj.identity_token.assert_called_once()

    def test_sign_wraps_signing_error_in_runtime_error(self, monkeypatch):
        fake_signer, _ = _install_fake_sigstore_sign(monkeypatch)
        fake_signer.sign_artifact.side_effect = ValueError("rekor down")
        signer = SigstoreSigner()
        with pytest.raises(RuntimeError, match="Sigstore signing failed"):
            signer.sign_artifact(b"a")

    def test_sign_import_error_when_sigstore_absent(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "sigstore", None)
        monkeypatch.setitem(sys.modules, "sigstore.sign", None)
        signer = SigstoreSigner()
        with pytest.raises(ImportError, match=r"novafabric\[sigstore\]"):
            signer.sign_artifact(b"a")


class TestSigstoreVerifyBundle:
    def test_verify_happy_path(self, monkeypatch):
        """Mock Bundle.from_json + Verifier.production + policy.UnsafeNoOp."""
        fake_bundle = MagicMock()
        models_mod = SimpleNamespace(Bundle=SimpleNamespace(from_json=lambda j: fake_bundle))

        fake_verifier = MagicMock()
        fake_verifier.verify_artifact.return_value = None
        verify_mod = SimpleNamespace(Verifier=SimpleNamespace(production=lambda: fake_verifier))
        policy_mod = SimpleNamespace(UnsafeNoOp=MagicMock)

        monkeypatch.setitem(sys.modules, "sigstore", SimpleNamespace())
        monkeypatch.setitem(sys.modules, "sigstore.models", models_mod)
        monkeypatch.setitem(sys.modules, "sigstore.verify", verify_mod)
        monkeypatch.setitem(sys.modules, "sigstore.verify.policy", policy_mod)

        signer = SigstoreSigner()
        bundle_dict = {
            "verificationMaterial": {
                "tlogEntries": [{"logIndex": "77"}],
                "x509CertificateChain": {"certificates": []},
            }
        }
        result = signer.verify_bundle(bundle_dict, b"artifact")
        assert result.valid is True
        assert result.rekor_log_index == 77
        fake_verifier.verify_artifact.assert_called_once()

    def test_verify_failure_returns_invalid_result(self, monkeypatch):
        fake_bundle = MagicMock()
        models_mod = SimpleNamespace(Bundle=SimpleNamespace(from_json=lambda j: fake_bundle))
        fake_verifier = MagicMock()
        fake_verifier.verify_artifact.side_effect = RuntimeError("sig invalid")
        verify_mod = SimpleNamespace(Verifier=SimpleNamespace(production=lambda: fake_verifier))
        policy_mod = SimpleNamespace(UnsafeNoOp=MagicMock)

        monkeypatch.setitem(sys.modules, "sigstore", SimpleNamespace())
        monkeypatch.setitem(sys.modules, "sigstore.models", models_mod)
        monkeypatch.setitem(sys.modules, "sigstore.verify", verify_mod)
        monkeypatch.setitem(sys.modules, "sigstore.verify.policy", policy_mod)

        signer = SigstoreSigner()
        result = signer.verify_bundle({"verificationMaterial": {}}, b"artifact")
        assert result.valid is False
        assert "sig invalid" in (result.error or "")

    def test_verify_import_error_returns_invalid_result(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "sigstore", None)
        monkeypatch.setitem(sys.modules, "sigstore.models", None)
        monkeypatch.setitem(sys.modules, "sigstore.verify", None)
        signer = SigstoreSigner()
        result = signer.verify_bundle({}, b"a")
        assert result.valid is False
        assert "novafabric[sigstore]" in (result.error or "")


class TestSigstoreIdentityExtraction:
    def _make_cert_b64(self, *, san_uri=None, san_email=None):
        import base64

        key = ec.generate_private_key(ec.SECP256R1())
        name = cx509.Name([cx509.NameAttribute(NameOID.COMMON_NAME, "fulcio-leaf")])
        builder = (
            cx509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(cx509.random_serial_number())
            .not_valid_before(datetime(2020, 1, 1, tzinfo=timezone.utc))
            .not_valid_after(datetime(2030, 1, 1, tzinfo=timezone.utc))
        )
        sans: list = []
        if san_uri:
            sans.append(x509.UniformResourceIdentifier(san_uri))
        if san_email:
            sans.append(x509.RFC822Name(san_email))
        if sans:
            builder = builder.add_extension(
                x509.SubjectAlternativeName(sans), critical=False
            )
        cert = builder.sign(key, hashes.SHA256())
        der = cert.public_bytes(Encoding.DER)
        return base64.b64encode(der).decode()

    def test_extract_identity_from_uri_san_in_chain(self):
        raw = self._make_cert_b64(san_uri="https://github.com/login/oauth")
        bundle = {
            "verificationMaterial": {
                "x509CertificateChain": {"certificates": [{"rawBytes": raw}]}
            }
        }
        assert _extract_identity(bundle) == "https://github.com/login/oauth"

    def test_extract_identity_from_email_san_leaf_field(self):
        raw = self._make_cert_b64(san_email="ci@example.com")
        bundle = {
            "verificationMaterial": {"certificate": {"rawBytes": raw}}
        }
        assert _extract_identity(bundle) == "ci@example.com"

    def test_extract_identity_falls_back_to_subject(self):
        raw = self._make_cert_b64()  # no SAN
        bundle = {"verificationMaterial": {"certificate": {"rawBytes": raw}}}
        ident = _extract_identity(bundle)
        assert ident is not None
        assert "fulcio-leaf" in ident

    def test_extract_identity_no_cert_returns_none(self):
        assert _extract_identity({"verificationMaterial": {}}) is None

    def test_extract_identity_garbage_returns_none(self):
        bundle = {
            "verificationMaterial": {"certificate": {"rawBytes": "bm90LWEtY2VydA=="}}
        }
        assert _extract_identity(bundle) is None

    def test_extract_rekor_log_index_present(self):
        bundle = {"verificationMaterial": {"tlogEntries": [{"logIndex": "1234"}]}}
        assert _extract_rekor_log_index(bundle) == 1234

    def test_extract_rekor_log_index_absent(self):
        assert _extract_rekor_log_index({"verificationMaterial": {}}) is None

    def test_extract_rekor_log_index_missing_field(self):
        bundle = {"verificationMaterial": {"tlogEntries": [{}]}}
        assert _extract_rekor_log_index(bundle) is None


# ===========================================================================
# SQLite MerkleLog — real temp SQLite (covers branches 217, 250, 436, 497)
# ===========================================================================

from novafabric.trust.novaseal.merkle import (  # noqa: E402
    MerkleError,
    MerkleLog,
    _root_from_parts,
    verify_consistency_proof,
)


class TestMerkleLogSqliteBranches:
    def test_root_from_empty_parts_returns_empty_hash(self):
        # Covers line 217 (the `if not parts:` branch).
        from novafabric.trust.novaseal.merkle import _empty_hash

        assert _root_from_parts([]) == _empty_hash()

    def test_verify_consistency_proof_empty_old_parts_false(self):
        # Covers line 250 (`if not old_parts: return False`).
        proof = {"old_parts": [], "tail_parts": []}
        assert verify_consistency_proof(proof, "a", "b") is False

    def test_verify_entry_root_none_returns_false(self, monkeypatch, tmp_path):
        """Covers line 436: verify_entry when current_root() is None."""
        log = MerkleLog(tmp_path / "m.db")
        log.append({"i": 0})
        monkeypatch.setattr(log, "current_root", lambda: None)
        assert log.verify_entry(0) is False
        log.close()

    def test_verify_consistency_no_tree_head_records_error(self, tmp_path):
        """Covers line 497: stored_head is None but len(rows) > 0."""
        log = MerkleLog(tmp_path / "m2.db")
        log.append({"i": 0})
        # Delete the tree_heads row to force the "no tree_head found" branch.
        log._conn.execute("DELETE FROM tree_heads")
        log._conn.commit()
        result = log.verify_consistency()
        assert result.consistent is False
        assert any("no tree_head found" in e for e in result.errors)
        log.close()

    def test_consistency_proof_roundtrip(self, tmp_path):
        log = MerkleLog(tmp_path / "m3.db")
        for i in range(6):
            log.append({"i": i})
        proof = log.consistency_proof(3, 6)
        assert verify_consistency_proof(proof, proof["old_root"], proof["new_root"]) is True
        log.close()

    def test_verify_consistency_clean_log(self, tmp_path):
        log = MerkleLog(tmp_path / "m4.db")
        for i in range(4):
            log.append({"i": i})
        result = log.verify_consistency()
        assert result.consistent is True
        assert result.leaf_count == 4
        log.close()


# ===========================================================================
# PostgresMerkleLog — mock psycopg connection/cursor (no real DB)
# ===========================================================================

from novafabric.trust.novaseal.merkle import (  # noqa: E402
    PostgresMerkleLog,
    _compute_root,
    _leaf_hash,
)


class _FakeCursor:
    """Minimal psycopg-cursor stand-in supporting context-manager + execute/fetch."""

    def __init__(self, store):
        self._store = store
        self._result: list = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        sql_norm = " ".join(sql.split())
        store = self._store
        if sql_norm.startswith("INSERT INTO nova_seal_leaves"):
            leaf_hash, entry_json = params
            idx = len(store["leaves"])
            store["leaves"].append((idx, leaf_hash, entry_json))
            self._result = [(idx,)]
        elif "SELECT leaf_hash FROM nova_seal_leaves ORDER BY leaf_index" in sql_norm:
            self._result = [(row[1],) for row in store["leaves"]]
        elif sql_norm.startswith("INSERT INTO nova_seal_tree_heads"):
            tree_size, root_hash = params
            store["heads"][tree_size] = root_hash
            self._result = []
        elif "SELECT root_hash FROM nova_seal_tree_heads ORDER BY tree_size DESC" in sql_norm:
            if store["heads"]:
                top = max(store["heads"])
                self._result = [(store["heads"][top],)]
            else:
                self._result = []
        elif "SELECT root_hash FROM nova_seal_tree_heads WHERE tree_size" in sql_norm:
            (size,) = params
            self._result = [(store["heads"][size],)] if size in store["heads"] else []
        elif "SELECT COUNT(*) FROM nova_seal_leaves" in sql_norm:
            self._result = [(len(store["leaves"]),)]
        elif "SELECT entry_json FROM nova_seal_leaves WHERE leaf_index" in sql_norm:
            (idx,) = params
            match = [r for r in store["leaves"] if r[0] == idx]
            self._result = [(match[0][2],)] if match else []
        elif "SELECT leaf_hash FROM nova_seal_leaves WHERE leaf_index" in sql_norm:
            (idx,) = params
            match = [r for r in store["leaves"] if r[0] == idx]
            self._result = [(match[0][1],)] if match else []
        elif "SELECT leaf_index, leaf_hash, entry_json FROM nova_seal_leaves WHERE leaf_index IN" in sql_norm:
            wanted = set(params)
            self._result = [r for r in store["leaves"] if r[0] in wanted]
        elif "SELECT leaf_index, leaf_hash, entry_json FROM nova_seal_leaves ORDER BY" in sql_norm:
            self._result = list(store["leaves"])
        elif sql_norm.startswith("CREATE"):
            self._result = []
        else:  # pragma: no cover - defensive
            self._result = []

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return list(self._result)

    def fetchmany(self, n):
        batch, self._result = self._result[:n], self._result[n:]
        return batch


class _FakeTransaction:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeConn:
    def __init__(self, store):
        self._store = store
        self.closed = False

    def cursor(self):
        return _FakeCursor(self._store)

    def transaction(self):
        return _FakeTransaction()

    def commit(self):
        pass

    def close(self):
        self.closed = True


@pytest.fixture
def pg_log(monkeypatch):
    """A PostgresMerkleLog whose _ensure_connected() installs a fake connection."""
    store = {"leaves": [], "heads": {}}
    log = PostgresMerkleLog("postgresql://fake/db")

    def _fake_ensure():
        if log._conn is None:
            log._conn = _FakeConn(store)

    monkeypatch.setattr(log, "_ensure_connected", _fake_ensure)
    return log, store


class TestPostgresMerkleLogMocked:
    def test_append_and_root(self, pg_log):
        log, _ = pg_log
        entry = log.append({"capsule_id": "c0"})
        assert entry["leaf_index"] == 0
        assert entry["tree_size"] == 1
        assert len(entry["leaf_hash"]) == 64
        assert log.current_root() == entry["root_hash"]

    def test_tree_size_and_get_leaf(self, pg_log):
        log, _ = pg_log
        assert log.tree_size() == 0
        log.append({"i": 0})
        log.append({"i": 1})
        assert log.tree_size() == 2
        leaf = log.get_leaf(1)
        assert leaf == {"i": 1}
        assert log.get_leaf(99) is None

    def test_current_root_none_when_empty(self, pg_log):
        log, _ = pg_log
        assert log.current_root() is None

    def test_get_leaf_hash_and_missing(self, pg_log):
        log, _ = pg_log
        for i in range(3):
            log.append({"i": i})
        h = log.get_leaf_hash(1)
        assert len(h) == 64
        with pytest.raises(MerkleError, match="not found"):
            log.get_leaf_hash(42)

    def test_inclusion_proof_and_verify_entry(self, pg_log):
        log, _ = pg_log
        for i in range(8):
            log.append({"i": i})
        proof = log.get_inclusion_proof(3)
        assert isinstance(proof, list)
        assert log.verify_entry(3) is True
        # Out-of-range index → False
        assert log.verify_entry(99) is False

    def test_verify_entry_root_none(self, pg_log, monkeypatch):
        log, _ = pg_log
        log.append({"i": 0})
        monkeypatch.setattr(log, "current_root", lambda: None)
        assert log.verify_entry(0) is False

    def test_consistency_proof_and_root_at_size(self, pg_log):
        log, _ = pg_log
        for i in range(6):
            log.append({"i": i})
        proof = log.consistency_proof(3, 6)
        assert verify_consistency_proof(proof, proof["old_root"], proof["new_root"]) is True
        # root_at_size hits the stored tree_heads
        assert log.root_at_size(6) is not None
        assert log.root_at_size(999) is None

    def test_verify_consistency_empty(self, pg_log):
        log, _ = pg_log
        result = log.verify_consistency()
        assert result.consistent is True
        assert result.leaf_count == 0

    def test_verify_consistency_small_log_all_checked(self, pg_log):
        log, _ = pg_log
        for i in range(10):
            log.append({"i": i})
        result = log.verify_consistency()
        assert result.consistent is True
        assert result.leaf_count == 10
        assert result.errors == []

    def test_verify_consistency_detects_tampered_leaf(self, pg_log):
        log, store = pg_log
        for i in range(5):
            log.append({"i": i})
        # Tamper a stored leaf_hash without updating entry_json.
        idx, _, entry_json = store["leaves"][2]
        store["leaves"][2] = (idx, "deadbeef" * 8, entry_json)
        result = log.verify_consistency()
        assert result.consistent is False
        assert any("leaf 2" in e for e in result.errors)

    def test_verify_consistency_missing_tree_head(self, pg_log):
        log, store = pg_log
        for i in range(3):
            log.append({"i": i})
        store["heads"].clear()  # drop all tree heads
        result = log.verify_consistency()
        assert result.consistent is False
        assert any("no tree_head found" in e for e in result.errors)

    def test_verify_consistency_root_mismatch(self, pg_log):
        log, store = pg_log
        for i in range(3):
            log.append({"i": i})
        # Corrupt the stored head root for size 3.
        store["heads"][3] = "0" * 64
        result = log.verify_consistency()
        assert result.consistent is False
        assert any("root mismatch" in e for e in result.errors)

    def test_verify_consistency_full_audit_large(self, pg_log, monkeypatch):
        """full=True with leaf_count > sample_size exercises the streaming
        re-hash branch (lines 778-797)."""
        monkeypatch.setattr(
            "novafabric.trust.novaseal.merkle._SAMPLE_SIZE", 2, raising=True
        )
        log, _ = pg_log
        for i in range(6):
            log.append({"i": i})
        result = log.verify_consistency(full=True)
        assert result.consistent is True
        assert result.leaf_count == 6

    def test_close_resets_connection(self, pg_log):
        log, _ = pg_log
        log.append({"i": 0})
        assert log._conn is not None
        log.close()
        assert log._conn is None


class TestPostgresMerkleLogEnsureConnected:
    def test_ensure_connected_import_error(self, monkeypatch):
        """_ensure_connected raises ImportError when psycopg is absent (line 574-579)."""
        log = PostgresMerkleLog("postgresql://fake/db")
        monkeypatch.setitem(sys.modules, "psycopg", None)
        with pytest.raises(ImportError, match=r"novafabric\[seal-postgres\]"):
            log._ensure_connected()

    def test_ensure_connected_real_psycopg_connect_mocked(self, monkeypatch):
        """_ensure_connected calls psycopg.connect + runs schema (lines 580-583)."""
        store = {"leaves": [], "heads": {}}
        fake_conn = _FakeConn(store)
        fake_psycopg = SimpleNamespace(connect=lambda dsn, autocommit: fake_conn)
        monkeypatch.setitem(sys.modules, "psycopg", fake_psycopg)

        log = PostgresMerkleLog("postgresql://fake/db")
        log._ensure_connected()
        assert log._conn is fake_conn
        # Second call is a no-op (already connected).
        log._ensure_connected()
        assert log._conn is fake_conn

    def test_close_swallows_exception(self):
        log = PostgresMerkleLog("postgresql://fake/db")

        class _BadConn:
            def close(self):
                raise RuntimeError("boom")

        log._conn = _BadConn()
        log.close()  # must not raise
        assert log._conn is None


def test_fake_cursor_matches_real_compute_root(pg_log):
    """Sanity: the fake backend's append produces the same root as _compute_root."""
    log, store = pg_log
    for i in range(5):
        log.append({"i": i})
    hashes_list = [r[1] for r in store["leaves"]]
    assert log.current_root() == _compute_root(hashes_list)
    # And the leaf hash matches the canonical hashing.
    import json as _json

    expected0 = _leaf_hash(
        _json.dumps({"i": 0}, sort_keys=True, separators=(",", ":")).encode()
    )
    assert store["leaves"][0][1] == expected0
