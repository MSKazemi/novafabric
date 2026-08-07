"""First-party listener TLS (ADR-0241 slice 1).

Covers the three layers the slice adds: config parsing + env overrides
(ADR-0029's documented ``tls.*`` / ``NOVA_TLS_*`` names, real for the first
time), fail-closed launch validation (missing material, world-readable key),
and a genuine HTTPS round-trip — uvicorn serving the real server app with a
self-signed certificate, verified by an httpx client pinned to that cert.
"""

from __future__ import annotations

import datetime
import ipaddress
import socket
import threading
import time
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from novafabric.server.config import (
    ServerConfig,
    TlsConfig,
    TlsConfigError,
    check_tls_config,
)


def _self_signed(tmp_path: Path, hostname: str = "127.0.0.1") -> tuple[Path, Path]:
    key = ec.generate_private_key(ec.SECP256R1())
    key_path = tmp_path / "tls.key"
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    key_path.chmod(0o600)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(hostname))]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path = tmp_path / "tls.crt"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return cert_path, key_path


class TestConfig:
    def test_default_is_off(self) -> None:
        cfg = ServerConfig()
        assert cfg.tls.enabled is False
        check_tls_config(cfg.tls)  # no-op when disabled

    def test_env_overrides(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        cert, key = _self_signed(tmp_path)
        monkeypatch.setenv("NOVA_TLS_ENABLED", "1")
        monkeypatch.setenv("NOVA_TLS_CERT_PATH", str(cert))
        monkeypatch.setenv("NOVA_TLS_KEY_PATH", str(key))
        cfg = ServerConfig()
        assert cfg.tls.enabled is True
        assert cfg.tls.cert_path == str(cert)
        check_tls_config(cfg.tls)  # valid material passes

    def test_enabled_without_paths_fails_closed(self) -> None:
        with pytest.raises(TlsConfigError, match="requires both"):
            check_tls_config(TlsConfig(enabled=True))

    def test_missing_files_fail_closed(self, tmp_path: Path) -> None:
        with pytest.raises(TlsConfigError, match="does not exist"):
            check_tls_config(
                TlsConfig(
                    enabled=True,
                    cert_path=str(tmp_path / "no.crt"),
                    key_path=str(tmp_path / "no.key"),
                )
            )

    def test_world_readable_key_fails_closed(self, tmp_path: Path) -> None:
        cert, key = _self_signed(tmp_path)
        key.chmod(0o644)
        with pytest.raises(TlsConfigError, match="chmod 600"):
            check_tls_config(
                TlsConfig(enabled=True, cert_path=str(cert), key_path=str(key))
            )


class TestServeCliValidation:
    def test_serve_refuses_half_configured_tls(self, tmp_path: Path) -> None:
        from typer.testing import CliRunner

        from novafabric.cli.main import app

        cert, _key = _self_signed(tmp_path)
        result = CliRunner().invoke(
            app, ["serve", "--experimental", "--tls-cert", str(cert)]
        )
        assert result.exit_code == 2
        assert "TLS configuration error" in result.output


@pytest.mark.timeout(60)
def test_real_https_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """uvicorn serves the real server app over HTTPS; a client pinned to the
    self-signed cert gets 200 from /livez. Plain HTTP against the same port
    fails — the listener is not accidentally plaintext."""
    httpx = pytest.importorskip("httpx")
    uvicorn = pytest.importorskip("uvicorn")

    from novafabric.server.app import create_app

    cert, key = _self_signed(tmp_path)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    cfg = ServerConfig(insecure_no_auth=True, db_path=str(tmp_path / "r.db"))
    app = create_app(cfg)
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="error",
            ssl_certfile=str(cert),
            ssl_keyfile=str(key),
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 30
        last_exc: Exception | None = None
        while time.monotonic() < deadline:
            try:
                resp = httpx.get(
                    f"https://127.0.0.1:{port}/livez", verify=str(cert), timeout=2.0
                )
                break
            except Exception as exc:  # noqa: BLE001 — startup polling
                last_exc = exc
                time.sleep(0.1)
        else:
            raise AssertionError(f"HTTPS server never came up: {last_exc}")
        assert resp.status_code == 200

        with pytest.raises(Exception):  # noqa: B017 — any TLS/protocol error is a pass
            httpx.get(f"http://127.0.0.1:{port}/livez", timeout=2.0)
    finally:
        server.should_exit = True
        thread.join(timeout=10)
