"""Tests for v0.27.0 seal bypass endpoint.

Covers:
  POST /api/seal/{capsule_id}/bypass — time-limited SoD bypass (ADR-0059)
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Iterator

import pytest

fastapi_installed = pytest.importorskip("fastapi")
pytest.importorskip("starlette")

from fastapi.testclient import TestClient  # noqa: E402

from novafabric.serve.app import create_app  # noqa: E402

VALID_TOKEN = "test-token-v027bypass"
H = {"host": "127.0.0.1:4321"}

LONG_REASON = "Emergency hotfix required due to critical security vulnerability. Authorized by CTO per incident response procedure IR-2026-001."


def _gen_key_cert() -> tuple[str, str]:
    """Generate an ECDSA P-256 key + self-signed PEM certificate."""
    pytest.importorskip("cryptography")
    from cryptography import x509  # noqa: PLC0415
    from cryptography.hazmat.primitives import hashes, serialization  # noqa: PLC0415
    from cryptography.hazmat.primitives.asymmetric import ec  # noqa: PLC0415
    from cryptography.x509.oid import NameOID  # noqa: PLC0415

    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test Admin")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC))
        .not_valid_after(datetime.now(UTC) + timedelta(days=365))
        .sign(key, hashes.SHA256())
    )
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    return key_pem, cert_pem


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    capsule_dir = tmp_path / "runs"
    capsule_dir.mkdir()
    app = create_app(
        capsule_dir=capsule_dir,
        token=VALID_TOKEN,
        db_path=tmp_path / "registry.db",
        static_dir=None,
    )
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# Validation error cases
# ---------------------------------------------------------------------------


def test_bypass_reason_too_short(client: TestClient) -> None:
    key_pem, cert_pem = _gen_key_cert()
    r = client.post(
        "/api/seal/run-001/bypass?token=" + VALID_TOKEN,
        json={"reason": "short", "duration_hours": 24, "key_pem": key_pem, "cert_pem": cert_pem},
        headers=H,
    )
    assert r.status_code == 422
    assert "reason" in r.text.lower()


def test_bypass_duration_zero(client: TestClient) -> None:
    key_pem, cert_pem = _gen_key_cert()
    r = client.post(
        "/api/seal/run-001/bypass?token=" + VALID_TOKEN,
        json={"reason": LONG_REASON, "duration_hours": 0, "key_pem": key_pem, "cert_pem": cert_pem},
        headers=H,
    )
    assert r.status_code == 422
    assert "duration_hours" in r.text.lower()


def test_bypass_duration_too_long(client: TestClient) -> None:
    key_pem, cert_pem = _gen_key_cert()
    r = client.post(
        "/api/seal/run-001/bypass?token=" + VALID_TOKEN,
        json={"reason": LONG_REASON, "duration_hours": 200, "key_pem": key_pem, "cert_pem": cert_pem},
        headers=H,
    )
    assert r.status_code == 422


def test_bypass_missing_key(client: TestClient) -> None:
    _, cert_pem = _gen_key_cert()
    r = client.post(
        "/api/seal/run-001/bypass?token=" + VALID_TOKEN,
        json={"reason": LONG_REASON, "duration_hours": 24, "key_pem": "", "cert_pem": cert_pem},
        headers=H,
    )
    assert r.status_code == 422
    assert "key_pem" in r.text.lower()


def test_bypass_missing_cert(client: TestClient) -> None:
    key_pem, _ = _gen_key_cert()
    r = client.post(
        "/api/seal/run-001/bypass?token=" + VALID_TOKEN,
        json={"reason": LONG_REASON, "duration_hours": 24, "key_pem": key_pem, "cert_pem": ""},
        headers=H,
    )
    assert r.status_code == 422
    assert "cert_pem" in r.text.lower()


def test_bypass_invalid_cert_pem(client: TestClient) -> None:
    key_pem, _ = _gen_key_cert()
    r = client.post(
        "/api/seal/run-001/bypass?token=" + VALID_TOKEN,
        json={"reason": LONG_REASON, "duration_hours": 24, "key_pem": key_pem, "cert_pem": "not-a-cert"},
        headers=H,
    )
    assert r.status_code == 422


def test_bypass_requires_auth(client: TestClient) -> None:
    r = client.post(
        "/api/seal/run-001/bypass",
        json={"reason": LONG_REASON, "duration_hours": 24, "key_pem": "k", "cert_pem": "c"},
        headers=H,
    )
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Successful bypass creation
# ---------------------------------------------------------------------------


def test_bypass_success(client: TestClient, tmp_path: Path) -> None:
    key_pem, cert_pem = _gen_key_cert()
    r = client.post(
        "/api/seal/run-abc/bypass?token=" + VALID_TOKEN,
        json={
            "reason": LONG_REASON,
            "duration_hours": 48,
            "key_pem": key_pem,
            "cert_pem": cert_pem,
            "target_env": "staging",
        },
        headers=H,
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["capsule_id"] == "run-abc"
    assert data["authorized_by"] == "Test Admin"
    assert data["target_env"] == "staging"
    assert "bypass_uuid" in data
    assert "valid_until" in data


def test_bypass_default_env_is_production(client: TestClient) -> None:
    key_pem, cert_pem = _gen_key_cert()
    r = client.post(
        "/api/seal/run-xyz/bypass?token=" + VALID_TOKEN,
        json={"reason": LONG_REASON, "duration_hours": 24, "key_pem": key_pem, "cert_pem": cert_pem},
        headers=H,
    )
    assert r.status_code == 200, r.text
    assert r.json()["target_env"] == "production"
