"""Maker-checker bypass notifications are sent (audit H3, 2026-10-22).

``build_notifier_from_env()`` had no caller and every ``PromoteBundleStore`` defaulted
to ``NullBypassNotifier``, so ``NOVA_BYPASS_NOTIFY_FILE`` and
``NOVA_BYPASS_NOTIFY_WEBHOOK`` did nothing: a bypass of the two-person rule went out
silently on every path. These tests drive the real surfaces (``nova seal bypass``
and ``POST /api/seal/{id}/bypass``) with the variables set, and pin that a failing
channel never blocks the bypass but is logged and recorded in the audit log.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from typer.testing import CliRunner

from novafabric.cli.main import app
from novafabric.promote.bundle_store import PromoteBundleStore

runner = CliRunner()
REASON = "Emergency bypass for incident INC-4242: the checker is unreachable and prod is down."
WEBHOOK_TOKEN = "hook-secret-T0K3N"


def _key_cert(tmp_path: Path) -> tuple[Path, Path, str, str]:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Bypass Operator")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_path, cert_path = tmp_path / "op.key", tmp_path / "op.crt"
    key_path.write_bytes(key_pem)
    cert_path.write_bytes(cert_pem)
    return key_path, cert_path, key_pem.decode(), cert_pem.decode()


class _Hook(BaseHTTPRequestHandler):
    received: list[dict[str, object]] = []

    def do_POST(self) -> None:  # noqa: N802 — http.server API
        length = int(self.headers.get("Content-Length", "0"))
        _Hook.received.append(json.loads(self.rfile.read(min(length, 65536))))
        self.send_response(204)
        self.end_headers()

    def log_message(self, *args: object) -> None:  # silence the test output
        pass


@pytest.fixture()
def webhook() -> Iterator[str]:
    _Hook.received = []
    server = HTTPServer(("127.0.0.1", 0), _Hook)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/hooks/{WEBHOOK_TOKEN}"
    finally:
        server.shutdown()
        server.server_close()


def _audit_entries(event_type: str) -> list[dict[str, object]]:
    path = Path(os.environ["NOVAFABRIC_AUDIT_LOG_PATH"])
    if not path.is_file():
        return []
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return [r for r in rows if r["event_type"] == event_type]


def _cli_bypass(tmp_path: Path, capsule_id: str = "cap-h3") -> object:
    key_path, cert_path, _, _ = _key_cert(tmp_path)
    return runner.invoke(
        app,
        [
            "seal", "bypass", capsule_id,
            "--reason", REASON,
            "--key", str(key_path),
            "--cert", str(cert_path),
            "--data-dir", str(tmp_path / "data"),
            "--db", str(tmp_path / "merkle.db"),
        ],
    )


def test_cli_bypass_writes_the_file_notification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sink = tmp_path / "notify" / "bypass.jsonl"
    monkeypatch.setenv("NOVA_BYPASS_NOTIFY_FILE", str(sink))

    result = _cli_bypass(tmp_path)

    assert result.exit_code == 0, result.output
    events = [json.loads(line) for line in sink.read_text().splitlines()]
    assert [e["event_type"] for e in events] == ["bypass_created"]
    stored = PromoteBundleStore(tmp_path / "data").list_bypasses("cap-h3")
    assert events[0]["bypass_uuid"] == stored[0]
    audited = _audit_entries("seal.bypass.notify")
    assert [a["details"]["outcome"] for a in audited] == ["delivered"]


def test_cli_bypass_posts_the_webhook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, webhook: str
) -> None:
    monkeypatch.setenv("NOVA_BYPASS_NOTIFY_WEBHOOK", webhook)

    result = _cli_bypass(tmp_path)

    assert result.exit_code == 0, result.output
    assert [e["event_type"] for e in _Hook.received] == ["bypass_created"]
    assert _Hook.received[0]["capsule_id"] == "cap-h3"


def test_serve_bypass_notifies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, webhook: str
) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from novafabric.serve.app import create_app

    sink = tmp_path / "bypass.jsonl"
    monkeypatch.setenv("NOVA_BYPASS_NOTIFY_FILE", str(sink))
    monkeypatch.setenv("NOVA_BYPASS_NOTIFY_WEBHOOK", webhook)
    _, _, key_pem, cert_pem = _key_cert(tmp_path)
    token = "test-token-h3-bypass"
    api = create_app(
        capsule_dir=tmp_path / "runs", token=token, db_path=tmp_path / "r.db", static_dir=None
    )
    with TestClient(api) as client:
        res = client.post(
            f"/api/seal/run-h3/bypass?token={token}",
            json={"reason": REASON, "duration_hours": 2, "key_pem": key_pem, "cert_pem": cert_pem},
            headers={"host": "127.0.0.1:4321"},
        )
    assert res.status_code == 200, res.text
    bypass_uuid = res.json()["bypass_uuid"]
    assert json.loads(sink.read_text())["bypass_uuid"] == bypass_uuid
    assert [e["bypass_uuid"] for e in _Hook.received] == [bypass_uuid]


def test_a_failing_notifier_never_blocks_the_bypass_but_is_logged_and_audited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    blocked = tmp_path / "not-a-file"
    blocked.mkdir()  # appending to a directory fails
    monkeypatch.setenv("NOVA_BYPASS_NOTIFY_FILE", str(blocked))
    monkeypatch.setenv(
        "NOVA_BYPASS_NOTIFY_WEBHOOK", f"http://127.0.0.1:1/hooks/{WEBHOOK_TOKEN}?k=v"
    )
    caplog.set_level(logging.WARNING)

    result = _cli_bypass(tmp_path)

    assert result.exit_code == 0, result.output
    assert PromoteBundleStore(tmp_path / "data").list_bypasses("cap-h3"), "bypass not stored"
    audited = _audit_entries("seal.bypass.notify")
    assert len(audited) == 1
    details = audited[0]["details"]
    assert details["outcome"] == "failed"
    assert sorted(details["failed_channels"]) == ["file", "webhook:http://127.0.0.1:1"]
    assert any("bypass notification failed" in r.getMessage() for r in caplog.records)
    # The webhook path and query can carry a credential; neither is recorded.
    everything = json.dumps(audited) + caplog.text + result.output
    assert WEBHOOK_TOKEN not in everything


def test_no_channel_configured_is_audited_as_not_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("NOVA_BYPASS_NOTIFY_FILE", raising=False)
    monkeypatch.delenv("NOVA_BYPASS_NOTIFY_WEBHOOK", raising=False)

    result = _cli_bypass(tmp_path)

    assert result.exit_code == 0, result.output
    audited = _audit_entries("seal.bypass.notify")
    assert [a["details"]["outcome"] for a in audited] == ["not_configured"]


def test_bypass_event_names_the_stored_bypass_bundle(tmp_path: Path) -> None:
    """L21: the human_approvals event's seal_bundle_path is the file actually stored."""
    from novafabric.capture.event_recorder import EventRecorder, set_current_recorder

    capsule = tmp_path / "active-capsule"
    capsule.mkdir()
    set_current_recorder(EventRecorder(capsule_dir=capsule, run_id="r", capsule_id="r"))
    try:
        result = _cli_bypass(tmp_path)
    finally:
        set_current_recorder(None)
    assert result.exit_code == 0, result.output  # type: ignore[attr-defined]
    events = [json.loads(ln) for ln in (capsule / "human_approvals.jsonl").read_text().splitlines()]
    assert [e["action"] for e in events] == ["bypassed"]
    bundle = Path(events[0]["seal_bundle_path"])
    assert bundle.is_file(), f"event points at nothing: {bundle}"
    stored = PromoteBundleStore(tmp_path / "data").list_bypasses("cap-h3")
    assert bundle == tmp_path / "data" / "promote" / "cap-h3" / "bypass" / f"{stored[0]}.json"
