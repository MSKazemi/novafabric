"""The URLs `nova serve` prints (and opens) are the ones it serves (review cli-b-19).

With ``--tls-cert/--tls-key`` the dashboard and API URLs — carrying the admin
token — were printed and opened as ``http://`` against an HTTPS listener; a
``--host ::1`` URL was printed unbracketed (``http://::1:4321``); and unusable TLS
material was detected only after the token was minted and the panel printed.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()


class _FakeApp:
    def __init__(self) -> None:
        self.state = type("State", (), {})()

    def include_router(self, router: object) -> None:
        pass

    def mount(self, *args: Any, **kwargs: Any) -> None:
        pass


@pytest.fixture
def served(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    calls: dict[str, Any] = {"tokens": 0}

    def _token() -> str:
        calls["tokens"] += 1
        return "tok-abc"

    monkeypatch.setattr("novafabric.serve.auth.generate_token", _token)
    monkeypatch.setattr("novafabric.serve.auth.write_token_file", lambda t: tmp_path / ".tok")
    monkeypatch.setattr("novafabric.serve.app.create_app", lambda **kw: _FakeApp())
    import uvicorn

    def _fake_run(app_obj: object, **kwargs: Any) -> None:
        calls["run"] = kwargs

    monkeypatch.setattr(uvicorn, "run", _fake_run)
    monkeypatch.delenv("NOVA_TLS_CERT_PATH", raising=False)
    monkeypatch.delenv("NOVA_TLS_KEY_PATH", raising=False)
    return calls


def _self_signed(tmp_path: Path) -> tuple[Path, Path]:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=30))
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))
    key_path.chmod(0o600)
    return cert_path, key_path


def _serve(tmp_path: Path, *extra: str) -> Any:
    return runner.invoke(
        app,
        ["serve", "--experimental", "--no-browser", "--capsule-dir", str(tmp_path / "caps"),
         *extra],
    )


def test_with_tls_every_printed_url_is_https(tmp_path: Path, served: dict[str, Any]) -> None:
    cert, key = _self_signed(tmp_path)
    result = _serve(tmp_path, "--tls-cert", str(cert), "--tls-key", str(key))
    assert result.exit_code == 0, result.output
    assert "https://127.0.0.1:" in result.output
    assert "http://127.0.0.1" not in result.output


def test_without_tls_the_urls_are_http(tmp_path: Path, served: dict[str, Any]) -> None:
    result = _serve(tmp_path)
    assert result.exit_code == 0, result.output
    assert "http://127.0.0.1:" in result.output


def test_an_ipv6_host_is_bracketed(tmp_path: Path, served: dict[str, Any]) -> None:
    result = _serve(tmp_path, "--host", "::1")
    assert result.exit_code == 0, result.output
    assert "http://[::1]:" in result.output
    assert "http://::1:" not in result.output


def test_unusable_tls_material_fails_before_a_token_is_minted(
    tmp_path: Path, served: dict[str, Any]
) -> None:
    result = _serve(tmp_path, "--tls-cert", str(tmp_path / "missing.pem"),
                    "--tls-key", str(tmp_path / "missing.key"))
    assert result.exit_code == 2, result.output
    assert served["tokens"] == 0
    assert "Dashboard:" not in result.output
