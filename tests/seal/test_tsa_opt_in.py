"""RFC 3161 timestamping is opt-in: no TSA URL configured means no network call.

Audit finding S4 (2026-10-01): once ``novaseal.yaml`` existed, an omitted ``tsa_url``
defaulted to ``https://freetsa.org/tsr``, so every sealed capture made an outbound HTTPS
request to a third party from a local-mode core path — while the documentation said
"omit to skip timestamps". ``tsa_url:`` with an empty value was worse: YAML null became
the literal string ``"None"``, which was then used as a URL. ADR-0292 makes timestamping
explicit.
"""

from __future__ import annotations

import datetime
import logging
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from novafabric.trust.novaseal.config import SigningProfile, _parse_profile


@pytest.fixture()
def material(tmp_path: Path) -> tuple[Path, Path]:
    key = ec.generate_private_key(ec.SECP256R1())
    key_path = tmp_path / "k.pem"
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "tsa-opt-in")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    cert_path = tmp_path / "c.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return key_path, cert_path


def _profile(tmp_path: Path, material: tuple[Path, Path], extra: str = "") -> SigningProfile:
    key_path, cert_path = material
    config = tmp_path / "novaseal.yaml"
    config.write_text(
        f"profile: local\nkey_path: {key_path}\ncert_path: {cert_path}\n"
        f"merkle_db: {tmp_path / 'm.db'}\n{extra}"
    )
    return _parse_profile(config)


def test_omitted_tsa_url_means_no_timestamping(tmp_path: Path, material: tuple[Path, Path]) -> None:
    profile = _profile(tmp_path, material)
    assert profile.tsa_url == ""
    assert profile.tsa_urls == []


def test_empty_tsa_url_means_no_timestamping(tmp_path: Path, material: tuple[Path, Path]) -> None:
    profile = _profile(tmp_path, material, "tsa_url:\n")
    assert profile.tsa_url == ""
    assert profile.tsa_urls == []


def test_explicit_tsa_url_is_used(tmp_path: Path, material: tuple[Path, Path]) -> None:
    profile = _profile(tmp_path, material, "tsa_url: https://tsa.example.internal/tsr\n")
    assert profile.tsa_url == "https://tsa.example.internal/tsr"
    assert profile.tsa_urls == ["https://tsa.example.internal/tsr"]


def test_tsa_urls_alone_opts_in(tmp_path: Path, material: tuple[Path, Path]) -> None:
    profile = _profile(
        tmp_path, material, "tsa_urls:\n  - https://a.example/tsr\n  - https://b.example/tsr\n"
    )
    assert profile.tsa_url == "https://a.example/tsr"
    assert profile.tsa_urls == ["https://a.example/tsr", "https://b.example/tsr"]


def test_dataclass_default_has_no_external_url() -> None:
    profile = SigningProfile(profile="local")
    assert profile.tsa_url == ""
    assert profile.tsa_urls == []


def test_sealing_without_tsa_makes_no_network_call_and_says_so(
    tmp_path: Path,
    material: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import novafabric.trust.novaseal as novaseal
    from novafabric.trust.novaseal import KeyConfig, NovaSeal

    def _no_network(*_a: object, **_k: object) -> bytes:
        raise AssertionError("request_timestamp must not be called without a TSA URL")

    monkeypatch.setattr(novaseal, "request_timestamp", _no_network)
    monkeypatch.setattr(novaseal, "_NO_TSA_NOTE_EMITTED", False)
    profile = _profile(tmp_path, material)
    key_path, cert_path = material
    seal = NovaSeal(
        KeyConfig("local", str(key_path), str(cert_path)),
        tsa_url=profile.tsa_url,
        tsa_urls=profile.tsa_urls,
        db_path=str(tmp_path / "m.db"),
    )
    with caplog.at_level(logging.INFO, logger="novafabric.trust.novaseal"):
        bundle = seal.seal({"run_id": "no-tsa"})
    assert bundle.tsr == b""
    assert any("without an RFC 3161 timestamp" in r.getMessage() for r in caplog.records)
