"""A seal bypass is refused unless its audit entry is written (adversarial review r6, 2026-10-23).

A bypass skips the two-person rule. Its compensating control is the audit trail: the
hash-chained ``seal.bypass.create`` entry, plus a notification. The review found four
holes, all reproduced:

1. An unwritable audit log stored the bypass anyway, with only a warning logged.
2. ``POST /api/seal/{id}/bypass`` wrote no audit entry of its own (no actor, no
   dashboard record).
3. The signed predicate always said ``notification_status: not_configured`` on the
   server, and ``sent`` from the CLI whenever ``--notify`` was given, although
   nothing sends email and delivery happens only after signing.
4. A webhook that answers one byte at a time held ``put_bypass`` far past its 5 s
   timeout, because the timeout bounded each socket read, not the delivery.

The decisions pinned here: no audit entry, no bypass (fail closed, nothing stored);
the server route audits like the CLI; the predicate signs the channel
*configuration* (``pending`` or ``not_configured``, never ``sent``) and the delivery
outcome goes to the audit log; webhook delivery has a total wall-clock deadline.
"""

from __future__ import annotations

import base64
import datetime
import json
import os
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from typer.testing import CliRunner

from novafabric.cli.main import app
from novafabric.promote.bundle_store import PromoteBundleStore
from novafabric.promote.bypass_notify import (
    BypassEvent,
    BypassNotifyError,
    NullBypassNotifier,
    WebhookBypassNotifier,
)
from novafabric.promote.exceptions import BypassAuditError

runner = CliRunner()
REASON = "Emergency bypass for incident INC-6060: the checker is unreachable and prod is down."
ENVELOPE = b'{"payloadType":"x","payload":"","signatures":[]}'


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


def _audit_entries(event_type: str) -> list[dict[str, object]]:
    path = Path(os.environ["NOVAFABRIC_AUDIT_LOG_PATH"])
    if not path.is_file():
        return []
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return [r for r in rows if r["event_type"] == event_type]


def _break_audit_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the audit log at a directory: every append fails with an OSError."""
    blocked = tmp_path / "audit-is-a-directory"
    blocked.mkdir()
    monkeypatch.setenv("NOVAFABRIC_AUDIT_LOG_PATH", str(blocked))
    return blocked


def _signed_predicate(store: PromoteBundleStore, capsule_id: str) -> dict[str, object]:
    (bypass_uuid,) = store.list_bypasses(capsule_id)
    envelope = json.loads(store.get_bypass(capsule_id, bypass_uuid))
    predicate: dict[str, object] = json.loads(base64.b64decode(envelope["payload"]))
    return predicate


def _cli_bypass(tmp_path: Path, *extra: str, capsule_id: str = "cap-r6") -> object:
    key_path, cert_path, _, _ = _key_cert(tmp_path)
    return runner.invoke(
        app,
        [
            "seal", "bypass", capsule_id,
            "--reason", REASON,
            "--key", str(key_path),
            "--cert", str(cert_path),
            "--data-dir", str(tmp_path / "data"),
            *extra,
        ],
    )


# ---------------------------------------------------------------------------
# (a) fail closed: no audit entry, no bypass
# ---------------------------------------------------------------------------


def test_store_refuses_the_bypass_and_stores_nothing_when_the_audit_append_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _break_audit_log(tmp_path, monkeypatch)
    store = PromoteBundleStore(tmp_path / "data", notifier=NullBypassNotifier())

    with pytest.raises(BypassAuditError):
        store.put_bypass("cap-x", ENVELOPE, "2099-01-01T00:00:00+00:00", actor="tester")

    assert store.list_bypasses("cap-x") == []
    bypass_dir = tmp_path / "data" / "promote" / "cap-x" / "bypass"
    leftovers = list(bypass_dir.iterdir()) if bypass_dir.exists() else []
    assert leftovers == [], f"a refused bypass left files behind: {leftovers}"


def test_store_does_not_notify_a_refused_bypass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _break_audit_log(tmp_path, monkeypatch)
    sent: list[BypassEvent] = []

    class _Recorder:
        def notify(self, event: BypassEvent) -> None:
            sent.append(event)

    store = PromoteBundleStore(tmp_path / "data", notifier=_Recorder())
    with pytest.raises(BypassAuditError):
        store.put_bypass("cap-x", ENVELOPE, "2099-01-01T00:00:00+00:00", actor="tester")
    assert sent == [], "a bypass that does not exist was announced"


def test_store_writes_one_create_entry_naming_actor_bundle_digest_and_channels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hashlib

    monkeypatch.setenv("NOVA_BYPASS_NOTIFY_FILE", str(tmp_path / "sink.jsonl"))
    store = PromoteBundleStore(tmp_path / "data")

    bypass_uuid = store.put_bypass(
        "cap-x", ENVELOPE, "2099-01-01T00:00:00+00:00", actor="tester", surface="cli"
    )

    (entry,) = _audit_entries("seal.bypass.create")
    assert entry["actor"] == "tester"
    assert entry["resource_id"] == "cap-x"
    details = entry["details"]
    assert isinstance(details, dict)
    assert details["bypass_uuid"] == bypass_uuid
    assert details["valid_until"] == "2099-01-01T00:00:00+00:00"
    assert details["envelope_sha256"] == hashlib.sha256(ENVELOPE).hexdigest()
    assert details["notification_channels"] == ["file"]
    assert details["surface"] == "cli"
    # The create entry precedes the delivery outcome in the chain.
    rows = [
        json.loads(line)
        for line in Path(os.environ["NOVAFABRIC_AUDIT_LOG_PATH"]).read_text().splitlines()
    ]
    assert [r["event_type"] for r in rows] == ["seal.bypass.create", "seal.bypass.notify"]


def test_cli_bypass_is_refused_when_the_audit_log_is_unwritable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _break_audit_log(tmp_path, monkeypatch)

    result = _cli_bypass(tmp_path)

    assert result.exit_code != 0, result.output  # type: ignore[attr-defined]
    # A clean refusal, not a traceback that happens to exit non-zero.
    assert isinstance(result.exception, SystemExit), result.exception  # type: ignore[attr-defined]
    assert "audit" in result.output.lower()  # type: ignore[attr-defined]
    assert PromoteBundleStore(tmp_path / "data").list_bypasses("cap-r6") == []


def test_cli_bypass_create_entry_names_the_cli_actor_and_signer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("NOVA_BYPASS_NOTIFY_FILE", raising=False)
    monkeypatch.delenv("NOVA_BYPASS_NOTIFY_WEBHOOK", raising=False)

    result = _cli_bypass(tmp_path)

    assert result.exit_code == 0, result.output  # type: ignore[attr-defined]
    (entry,) = _audit_entries("seal.bypass.create")
    assert entry["actor"] == "cli-user"
    details = entry["details"]
    assert isinstance(details, dict)
    assert details["authorized_by"] == "Bypass Operator"
    assert details["surface"] == "cli"


# ---------------------------------------------------------------------------
# (b) the server route audits like the CLI
# ---------------------------------------------------------------------------


@pytest.fixture()
def serve_client(tmp_path: Path) -> Iterator[tuple[object, str]]:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from novafabric.serve.app import create_app

    token = "test-token-r6-bypass-audit"
    api = create_app(
        capsule_dir=tmp_path / "runs", token=token, db_path=tmp_path / "r.db", static_dir=None
    )
    with TestClient(api) as client:
        yield client, token


def _serve_bypass(client: object, token: str, tmp_path: Path) -> object:
    _, _, key_pem, cert_pem = _key_cert(tmp_path)
    return client.post(  # type: ignore[attr-defined]
        f"/api/seal/run-r6/bypass?token={token}",
        json={"reason": REASON, "duration_hours": 2, "key_pem": key_pem, "cert_pem": cert_pem},
        headers={"host": "127.0.0.1:4321"},
    )


def test_serve_bypass_writes_the_chained_entry_and_a_dashboard_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, serve_client: tuple[object, str]
) -> None:
    from novafabric.serve import audit as dashboard_audit

    monkeypatch.delenv("NOVA_BYPASS_NOTIFY_FILE", raising=False)
    monkeypatch.delenv("NOVA_BYPASS_NOTIFY_WEBHOOK", raising=False)
    client, token = serve_client

    res = _serve_bypass(client, token, tmp_path)

    assert res.status_code == 200, res.text  # type: ignore[attr-defined]
    bypass_uuid = res.json()["bypass_uuid"]  # type: ignore[attr-defined]
    (entry,) = _audit_entries("seal.bypass.create")
    assert str(entry["actor"]).startswith("serve:")
    assert entry["actor"] != "serve:"
    details = entry["details"]
    assert isinstance(details, dict)
    assert details["bypass_uuid"] == bypass_uuid
    assert details["surface"] == "serve"
    assert details["authorized_by"] == "Bypass Operator"
    records = [r for r in dashboard_audit.read_recent(50) if r["action"] == "seal_bypass"]
    assert len(records) == 1
    assert records[0]["result"] == "ok"
    assert records[0]["args"]["bypass_uuid"] == bypass_uuid
    assert "key_pem" not in json.dumps(records[0])


def test_serve_bypass_is_refused_with_503_when_the_audit_log_is_unwritable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, serve_client: tuple[object, str]
) -> None:
    from novafabric import _paths
    from novafabric.serve import audit as dashboard_audit

    client, token = serve_client
    _break_audit_log(tmp_path, monkeypatch)

    res = _serve_bypass(client, token, tmp_path)

    assert res.status_code == 503, res.text  # type: ignore[attr-defined]
    assert "audit" in res.text.lower()  # type: ignore[attr-defined]
    assert PromoteBundleStore(_paths.nova_home()).list_bypasses("run-r6") == []
    records = [r for r in dashboard_audit.read_recent(50) if r["action"] == "seal_bypass"]
    assert [r["result"] for r in records] == ["error"]


# ---------------------------------------------------------------------------
# (c) the signature covers the channel configuration, never a claimed delivery
# ---------------------------------------------------------------------------


def test_cli_predicate_says_not_configured_when_no_channel_is_set_even_with_notify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("NOVA_BYPASS_NOTIFY_FILE", raising=False)
    monkeypatch.delenv("NOVA_BYPASS_NOTIFY_WEBHOOK", raising=False)

    result = _cli_bypass(tmp_path, "--notify", "oncall@example.org")

    assert result.exit_code == 0, result.output  # type: ignore[attr-defined]
    predicate = _signed_predicate(PromoteBundleStore(tmp_path / "data"), "cap-r6")
    # It said "sent" here, although NovaFabric sends no email and nothing was relayed.
    assert predicate["notification_status"] == "not_configured"
    assert predicate["notification_sent_to"] == ["mailto:oncall@example.org"]


def test_cli_predicate_says_pending_and_names_the_configured_channels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOVA_BYPASS_NOTIFY_FILE", str(tmp_path / "sink.jsonl"))
    monkeypatch.setenv("NOVA_BYPASS_NOTIFY_WEBHOOK", "http://127.0.0.1:1/hook?token=SECRET")

    result = _cli_bypass(tmp_path)

    assert result.exit_code == 0, result.output  # type: ignore[attr-defined]
    predicate = _signed_predicate(PromoteBundleStore(tmp_path / "data"), "cap-r6")
    assert predicate["notification_status"] == "pending"
    assert predicate["notification_sent_to"] == ["file", "webhook:http://127.0.0.1:1"]
    assert "SECRET" not in json.dumps(predicate)
    # The outcome of the delivery that followed the signature is in the audit log.
    (notify,) = _audit_entries("seal.bypass.notify")
    details = notify["details"]
    assert isinstance(details, dict)
    assert details["outcome"] == "failed"  # the webhook port is closed


def test_serve_predicate_reflects_the_configured_channels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, serve_client: tuple[object, str]
) -> None:
    from novafabric import _paths

    monkeypatch.setenv("NOVA_BYPASS_NOTIFY_FILE", str(tmp_path / "sink.jsonl"))
    client, token = serve_client

    res = _serve_bypass(client, token, tmp_path)

    assert res.status_code == 200, res.text  # type: ignore[attr-defined]
    predicate = _signed_predicate(PromoteBundleStore(_paths.nova_home()), "run-r6")
    assert predicate["notification_status"] == "pending"
    assert predicate["notification_sent_to"] == ["file"]
    assert res.json()["notification_status"] == "pending"  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# (d) webhook delivery has a total wall-clock deadline
# ---------------------------------------------------------------------------


@pytest.fixture()
def drip_server() -> Iterator[str]:
    """An HTTP endpoint that answers one byte every 0.25 s (about 4 s for the reply)."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    stop = threading.Event()

    def drip() -> None:
        try:
            conn, _ = srv.accept()
        except OSError:
            return
        with conn:
            conn.settimeout(10)
            try:
                conn.recv(65536)
                for b in b"HTTP/1.1 200 OK\r\n":
                    if stop.is_set():
                        return
                    conn.sendall(bytes([b]))
                    time.sleep(0.25)
                conn.sendall(b"Content-Length: 0\r\n\r\n")
            except OSError:
                return

    thread = threading.Thread(target=drip, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{srv.getsockname()[1]}/hook?token=SECRET"
    finally:
        stop.set()
        srv.close()
        thread.join(timeout=10)


def test_a_slow_drip_webhook_is_cut_off_at_the_total_deadline(drip_server: str) -> None:
    notifier = WebhookBypassNotifier(drip_server, timeout_s=1.0)
    event = BypassEvent(
        event_type="bypass_created",
        capsule_id="cap-drip",
        bypass_uuid="u-drip",
        valid_until="2099-01-01T00:00:00+00:00",
    )

    started = time.monotonic()
    with pytest.raises(BypassNotifyError) as excinfo:
        notifier.notify(event)
    elapsed = time.monotonic() - started

    # Each byte arrives well inside the per-read timeout, so before the fix this ran
    # for the whole ~4 s drip and then reported success.
    assert elapsed < 2.5, f"delivery ran {elapsed:.1f}s against a 1 s deadline"
    assert "deadline" in str(excinfo.value)
    assert "SECRET" not in str(excinfo.value)
