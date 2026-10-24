"""The bypass webhook is posted off the event loop.

``POST /api/seal/{capsule_id}/bypass`` called ``PromoteBundleStore.put_bypass``
directly inside the ``async`` handler. ``put_bypass`` notifies synchronously —
``urllib.request.urlopen`` with a 5 s timeout for ``NOVA_BYPASS_NOTIFY_WEBHOOK`` —
so a slow or unreachable webhook froze every other request the server was
handling (probes, the dashboard, other users) for up to 5 s per bypass. The
store write and the notification now run in a worker thread; the response still
waits for the notification outcome, which is audited.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
httpx = pytest.importorskip("httpx")

from novafabric.promote import bundle_store as bundle_store_mod  # noqa: E402
from novafabric.promote.bypass_notify import BypassEvent  # noqa: E402
from novafabric.serve.app import create_app  # noqa: E402

TOKEN = "test-token-bypass-off-loop"
REASON = (
    "Emergency hotfix required due to a critical security vulnerability; authorized "
    "by the CTO per incident response procedure IR-2026-001."
)
SLOW_S = 1.5


def _key_cert() -> tuple[str, str]:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Bypass Admin")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    return (
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode(),
        cert.public_bytes(serialization.Encoding.PEM).decode(),
    )


class SlowWebhook:
    """A notifier that blocks like a slow webhook POST (urlopen is blocking IO)."""

    def __init__(self) -> None:
        self.events: list[BypassEvent] = []

    def notify(self, event: BypassEvent) -> None:
        time.sleep(SLOW_S)
        self.events.append(event)


def test_slow_bypass_webhook_does_not_block_other_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOVAFABRIC_HOME", str(tmp_path / "home"))
    slow = SlowWebhook()
    monkeypatch.setattr(bundle_store_mod, "build_notifier_from_env", lambda: slow)
    caps = tmp_path / "runs"
    caps.mkdir()
    from _promote_capsule import make_capsule

    make_capsule("run-001", caps)  # a bypass signs the capsule's digest (cli-b-01)
    app = create_app(token=TOKEN, capsule_dir=caps, db_path=tmp_path / "r.db")
    key_pem, cert_pem = _key_cert()

    async def scenario() -> tuple[float, float, float, int]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://127.0.0.1:4321"
        ) as client:
            t0 = time.monotonic()
            done: dict[str, float] = {}

            async def bypass() -> int:
                r = await client.post(
                    f"/api/seal/run-001/bypass?token={TOKEN}",
                    json={
                        "reason": REASON,
                        "duration_hours": 2,
                        "key_pem": key_pem,
                        "cert_pem": cert_pem,
                    },
                )
                done["bypass"] = time.monotonic()
                return r.status_code

            async def probe() -> float:
                await asyncio.sleep(0.3)  # let the bypass reach its notification
                start = time.monotonic()
                r = await client.get("/livez")
                assert r.status_code == 200
                done["probe"] = time.monotonic()
                return done["probe"] - start

            status, probe_latency = await asyncio.gather(bypass(), probe())
            return done["probe"] - t0, done["bypass"] - t0, probe_latency, status

    probe_at, bypass_at, probe_latency, status = asyncio.run(scenario())
    assert status == 200
    assert len(slow.events) == 1  # the notification still happened, once
    assert probe_at < bypass_at, "the probe waited for the bypass webhook"
    assert probe_latency < SLOW_S / 2, f"/livez took {probe_latency:.2f}s behind the webhook"
