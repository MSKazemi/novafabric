"""Tests for the webhook delivery dispatcher (ADR-0205 D4/D5, experimental).

Covers, against a real in-test HTTP receiver:
- delivery success: signed body, `X-NovaFabric-*` headers, signature verifies
  (manual HMAC recompute + reference verifier), delivery row updated
- matching: event-type filter, disabled flag, workspace scoping
- failure → bounded retries per the injectable schedule → dead-lettered after
  ``max_attempts`` with one audit entry per attempt
- queue overflow → drop-with-audit (one entry per window), request path never
  raises
- redeliver: terminal-failed row re-posts the stored payload on the same row
- ping: synthetic ``webhook.ping`` through the full path, bypassing the filter
- end-to-end REST: ping + redeliver over /v0/webhooks with the dispatcher
  started by the app lifespan; a dead endpoint never delays a capsule upload
"""

from __future__ import annotations

import hashlib
import hmac as hmac_module
import io
import json
import threading
import time
import zipfile
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
import yaml

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from novafabric.audit import AuditLog  # noqa: E402
from novafabric.events.model import (  # noqa: E402
    EventType,
    LifecycleEvent,
    Subject,
    SubjectKind,
)
from novafabric.server import webhooks as store  # noqa: E402
from novafabric.server.app import create_app  # noqa: E402
from novafabric.server.config import ServerConfig, WebhooksConfig  # noqa: E402
from novafabric.server.webhook_dispatch import (  # noqa: E402
    DispatchConfig,
    WebhookDispatcher,
)

TEST_TOKEN = "test-local-token-adr0205-dispatch"

#: All-immediate schedule — the spec shape is fixed but injectable for tests.
FAST_SCHEDULE = (0.0, 0.0, 0.0, 0.0, 0.0)


@pytest.fixture(autouse=True)
def _tmp_audit_log(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    path = tmp_path / "audit.jsonl"
    from novafabric.audit import _paths

    monkeypatch.setattr(_paths, "AUDIT_LOG_PATH", path)
    return path


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "registry.db"


class _Receiver:
    def __init__(self, server: ThreadingHTTPServer, url: str) -> None:
        self.server = server
        self.url = url
        self.received: list[dict[str, Any]] = []


@pytest.fixture
def receiver() -> Iterator[_Receiver]:
    """Local HTTP server recording every POST (tests/events/conftest.py shape)."""
    received: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 — http.server API
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            received.append({"body": body, "headers": dict(self.headers)})
            codes = getattr(self.server, "response_codes", None)
            if codes:
                code = codes.pop(0)
            else:
                code = getattr(self.server, "response_code", 200)
            self.send_response(code)
            self.end_headers()

        def log_message(self, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    hook = _Receiver(server, f"http://127.0.0.1:{server.server_port}/hook")
    hook.received = received
    yield hook
    server.shutdown()
    server.server_close()


def _wait_for(predicate: Callable[[], bool], timeout: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _event(
    event_type: EventType = EventType.CAPSULE_CREATED, ref: str = "run-abc"
) -> LifecycleEvent:
    return LifecycleEvent(
        type=event_type,
        subject=Subject(kind=SubjectKind.CAPSULE, ref=ref),
        payload={"status": "success"},
    )


@pytest.fixture
def dispatcher(db: Path) -> Iterator[WebhookDispatcher]:
    d = WebhookDispatcher(
        db_path=db,
        config=DispatchConfig(schedule_s=FAST_SCHEDULE, timeout_s=2.0),
    )
    d.start()
    yield d
    d.stop()


def _rows(db: Path, hook_id: str, status: str | None = None) -> list[dict[str, Any]]:
    return store.list_deliveries(hook_id, status=status, db_path=db)


# ---------------------------------------------------------------------------
# Delivery success path
# ---------------------------------------------------------------------------


class TestDeliverySuccess:
    def test_signed_delivery_reaches_receiver(
        self, db: Path, dispatcher: WebhookDispatcher, receiver: _Receiver
    ) -> None:
        secret, record = store.create_webhook(receiver.url, actor="test", db_path=db)
        hook_id = record["hook_id"]
        event = _event()

        dispatcher.enqueue_event(event)
        assert _wait_for(lambda: bool(_rows(db, hook_id, "delivered")))

        assert len(receiver.received) == 1
        request = receiver.received[0]
        body = request["body"]
        payload = json.loads(body)
        assert payload["type"] == "capsule.created"
        assert payload["event_id"] == event.event_id

        # Headers per the spec table.
        row = _rows(db, hook_id)[0]
        assert request["headers"]["X-NovaFabric-Webhook-Id"] == hook_id
        assert request["headers"]["X-NovaFabric-Delivery-Id"] == row["delivery_id"]
        assert request["headers"]["X-NovaFabric-Event-Id"] == event.event_id
        assert request["headers"]["X-NovaFabric-Event-Type"] == "capsule.created"

        # Signature: t=...,v1=... over "{t}.{body}" — recompute the HMAC here.
        header = request["headers"]["X-NovaFabric-Signature"]
        parts = dict(p.split("=", 1) for p in header.split(","))
        t = int(parts["t"])
        expected = hmac_module.new(
            secret.encode(), f"{t}.".encode() + body, hashlib.sha256
        ).hexdigest()
        assert parts["v1"] == expected
        assert store.verify_delivery_signature(secret, body, header)

        # Delivery row: exactly one POST, terminal delivered, HTTP 200.
        assert row["status"] == "delivered"
        assert row["attempts"] == 1
        assert row["last_status_code"] == 200
        assert row["next_attempt_at"] is None
        # The signing secret never appears in the stored row.
        assert secret not in json.dumps(row)

    def test_delivery_is_audited(
        self,
        db: Path,
        dispatcher: WebhookDispatcher,
        receiver: _Receiver,
        tmp_path: Path,
    ) -> None:
        _, record = store.create_webhook(receiver.url, actor="test", db_path=db)
        event = _event()
        dispatcher.enqueue_event(event)
        assert _wait_for(lambda: bool(_rows(db, record["hook_id"], "delivered")))
        log = AuditLog(tmp_path / "audit.jsonl")
        entries = [
            e
            for e in log.query(resource_id=event.event_id)
            if e.event_type.value == "webhook.delivery"
        ]
        assert len(entries) == 1
        assert entries[0].details["outcome"] == "delivered"


# ---------------------------------------------------------------------------
# Matching: type filter, disabled, workspace scope
# ---------------------------------------------------------------------------


class TestMatching:
    def test_event_type_filter(
        self, db: Path, dispatcher: WebhookDispatcher, receiver: _Receiver
    ) -> None:
        _, filtered = store.create_webhook(
            receiver.url,
            actor="test",
            event_types=["promotion.approved"],
            db_path=db,
        )
        dispatcher.enqueue_event(_event(EventType.CAPSULE_CREATED))
        dispatcher.enqueue_event(_event(EventType.PROMOTION_APPROVED, ref="promo-1"))
        assert _wait_for(lambda: bool(_rows(db, filtered["hook_id"], "delivered")))
        rows = _rows(db, filtered["hook_id"])
        assert [r["event_type"] for r in rows] == ["promotion.approved"]
        assert len(receiver.received) == 1

    def test_disabled_hook_receives_nothing(
        self, db: Path, dispatcher: WebhookDispatcher, receiver: _Receiver
    ) -> None:
        _, record = store.create_webhook(
            receiver.url, actor="test", disabled=True, db_path=db
        )
        dispatcher.enqueue_event(_event())
        time.sleep(0.5)
        assert _rows(db, record["hook_id"]) == []
        assert receiver.received == []

    def test_workspace_scoping(
        self, db: Path, dispatcher: WebhookDispatcher, receiver: _Receiver
    ) -> None:
        from novafabric.server import workspace_store

        workspace_store.ensure_default(db_path=db)
        ws = workspace_store.list_workspaces(db_path=db)[0]["slug"]
        _, scoped = store.create_webhook(
            receiver.url, actor="test", workspace=ws, db_path=db
        )
        _, unscoped = store.create_webhook(receiver.url, actor="test", db_path=db)

        # Unattributed events reach only the unscoped webhook (honesty bound).
        dispatcher.enqueue_event(_event(ref="run-unattributed"))
        # Attributed events reach both the matching scoped hook and unscoped.
        dispatcher.enqueue_event(_event(ref="run-scoped"), workspace=ws)
        # Attribution to a different workspace never reaches this scoped hook.
        dispatcher.enqueue_event(_event(ref="run-other"), workspace="other-ws")

        assert _wait_for(
            lambda: len(_rows(db, unscoped["hook_id"], "delivered")) == 3
        )
        scoped_rows = _rows(db, scoped["hook_id"])
        assert len(scoped_rows) == 1
        assert json.loads(scoped_rows[0]["payload"])["subject"]["ref"] == "run-scoped"


# ---------------------------------------------------------------------------
# Failure → retries per schedule → dead-letter after max_attempts
# ---------------------------------------------------------------------------


class TestRetriesAndDeadLetter:
    def test_dead_letter_after_max_attempts(
        self, db: Path, dispatcher: WebhookDispatcher, receiver: _Receiver, tmp_path: Path
    ) -> None:
        receiver.server.response_code = 500  # type: ignore[attr-defined]
        _, record = store.create_webhook(receiver.url, actor="test", db_path=db)
        event = _event()
        dispatcher.enqueue_event(event)

        assert _wait_for(lambda: bool(_rows(db, record["hook_id"], "failed")))
        row = _rows(db, record["hook_id"])[0]
        assert row["status"] == "failed"
        assert row["attempts"] == 5  # normative 5-attempt schedule
        assert row["next_attempt_at"] is None
        assert row["last_status_code"] == 500
        assert row["last_error"]
        assert len(receiver.received) == 5

        # One hash-chained audit entry per attempted delivery.
        log = AuditLog(tmp_path / "audit.jsonl")
        entries = [
            e
            for e in log.query(resource_id=event.event_id)
            if e.event_type.value == "webhook.delivery"
        ]
        assert len(entries) == 5
        assert [e.details["attempt"] for e in entries] == [1, 2, 3, 4, 5]
        assert all(e.details["outcome"] == "error" for e in entries)

    def test_retry_then_success(
        self, db: Path, dispatcher: WebhookDispatcher, receiver: _Receiver
    ) -> None:
        receiver.server.response_codes = [500, 500, 200]  # type: ignore[attr-defined]
        _, record = store.create_webhook(receiver.url, actor="test", db_path=db)
        dispatcher.enqueue_event(_event())
        assert _wait_for(lambda: bool(_rows(db, record["hook_id"], "delivered")))
        row = _rows(db, record["hook_id"])[0]
        assert row["attempts"] == 3
        assert row["last_status_code"] == 200

    def test_schedule_is_respected(self, db: Path, receiver: _Receiver) -> None:
        """A non-zero second slot delays attempt 2 by that offset (injectable)."""
        receiver.server.response_codes = [500, 200]  # type: ignore[attr-defined]
        d = WebhookDispatcher(
            db_path=db,
            config=DispatchConfig(
                schedule_s=(0.0, 0.6, 0.6, 0.6, 0.6), timeout_s=2.0
            ),
        )
        d.start()
        try:
            _, record = store.create_webhook(receiver.url, actor="test", db_path=db)
            start = time.monotonic()
            d.enqueue_event(_event())
            assert _wait_for(lambda: bool(_rows(db, record["hook_id"], "delivered")))
            elapsed = time.monotonic() - start
            assert elapsed >= 0.6  # attempt 2 waited for its schedule slot
            row = _rows(db, record["hook_id"])[0]
            assert row["attempts"] == 2
        finally:
            d.stop()

    def test_retrying_state_persists_next_attempt_at(
        self, db: Path, receiver: _Receiver
    ) -> None:
        receiver.server.response_code = 500  # type: ignore[attr-defined]
        d = WebhookDispatcher(
            db_path=db,
            config=DispatchConfig(schedule_s=(0.0, 30.0, 30.0, 30.0, 30.0), timeout_s=2.0),
        )
        d.start()
        try:
            _, record = store.create_webhook(receiver.url, actor="test", db_path=db)
            d.enqueue_event(_event())
            assert _wait_for(lambda: bool(_rows(db, record["hook_id"], "retrying")))
            row = _rows(db, record["hook_id"])[0]
            assert row["status"] == "retrying"
            assert row["attempts"] == 1
            assert row["next_attempt_at"] is not None  # persisted on the row
        finally:
            d.stop()


# ---------------------------------------------------------------------------
# Queue overflow: drop-with-audit, never block, never raise
# ---------------------------------------------------------------------------


class TestQueueOverflow:
    def test_overflow_drops_with_single_audit_per_window(
        self, db: Path, tmp_path: Path
    ) -> None:
        # Worker NOT started → the bounded queue fills and stays full.
        d = WebhookDispatcher(
            db_path=db,
            config=DispatchConfig(queue_max=1, schedule_s=FAST_SCHEDULE),
        )
        for i in range(4):
            d.enqueue_event(_event(ref=f"run-{i}"))  # must never raise

        assert d.dropped_count == 3
        log = AuditLog(tmp_path / "audit.jsonl")
        overflow = [
            e
            for e in log.query()
            if e.event_type.value == "webhook.queue.overflow"
        ]
        assert len(overflow) == 1  # one entry per bounded window
        assert overflow[0].details["queue_max"] == 1
        assert log.verify() == []

    def test_enqueue_never_raises_even_if_queue_is_broken(self, db: Path) -> None:
        d = WebhookDispatcher(db_path=db)

        class _Boom:
            def put_nowait(self, item: Any) -> None:
                raise RuntimeError("boom")

        d._queue = _Boom()  # type: ignore[assignment]
        d.enqueue_event(_event())  # swallowed, logged — the D4 invariant


# ---------------------------------------------------------------------------
# Ping + redeliver through the dispatcher
# ---------------------------------------------------------------------------


class TestPingAndRedeliver:
    def test_ping_delivers_synthetic_event(
        self, db: Path, dispatcher: WebhookDispatcher, receiver: _Receiver
    ) -> None:
        # Even a hook filtered away from webhook.ping is testable (targeted).
        secret, record = store.create_webhook(
            receiver.url, actor="test", event_types=["capsule.created"], db_path=db
        )
        delivery_id = dispatcher.ping(record["hook_id"], requested_by="admin@x")
        assert _wait_for(
            lambda: store.get_delivery(delivery_id, db_path=db)["status"]
            == "delivered"
        )
        body = receiver.received[0]["body"]
        payload = json.loads(body)
        assert payload["type"] == "webhook.ping"
        assert payload["payload"] == {
            "hook_id": record["hook_id"],
            "requested_by": "admin@x",
        }
        header = receiver.received[0]["headers"]["X-NovaFabric-Signature"]
        assert store.verify_delivery_signature(secret, body, header)

    def test_ping_unknown_hook_raises(
        self, db: Path, dispatcher: WebhookDispatcher
    ) -> None:
        with pytest.raises(store.UnknownWebhookError):
            dispatcher.ping("nosuchid", requested_by="admin@x")

    def test_redeliver_reposts_stored_payload_on_same_row(
        self, db: Path, receiver: _Receiver, tmp_path: Path
    ) -> None:
        receiver.server.response_code = 500  # type: ignore[attr-defined]
        d = WebhookDispatcher(
            db_path=db,
            config=DispatchConfig(schedule_s=FAST_SCHEDULE, max_attempts=2, timeout_s=2.0),
        )
        d.start()
        try:
            _, record = store.create_webhook(receiver.url, actor="test", db_path=db)
            d.enqueue_event(_event())
            assert _wait_for(lambda: bool(_rows(db, record["hook_id"], "failed")))
            row = _rows(db, record["hook_id"])[0]
            assert row["attempts"] == 2
            first_body = receiver.received[0]["body"]

            receiver.server.response_code = 200  # type: ignore[attr-defined]
            d.redeliver(record["hook_id"], row["delivery_id"], actor="admin@x")
            assert _wait_for(
                lambda: store.get_delivery(row["delivery_id"], db_path=db)["status"]
                == "delivered"
            )
            after = store.get_delivery(row["delivery_id"], db_path=db)
            # Same row, fresh chain: attempt history accumulates, marker set.
            assert after["attempts"] == 3
            assert after["redelivery_of"] == row["delivery_id"]
            # The STORED payload was re-posted byte-identically.
            assert receiver.received[-1]["body"] == first_body

            log = AuditLog(tmp_path / "audit.jsonl")
            redelivers = [
                e
                for e in log.query(resource_id=row["delivery_id"])
                if e.event_type.value == "webhook.redeliver"
            ]
            assert len(redelivers) == 1
            assert redelivers[0].actor == "admin@x"
        finally:
            d.stop()

    def test_redeliver_non_terminal_raises(
        self, db: Path, dispatcher: WebhookDispatcher
    ) -> None:
        _, record = store.create_webhook(URL_HTTPS, actor="test", db_path=db)
        delivery_id = store.insert_delivery(
            record["hook_id"],
            event_id="e1",
            event_type="capsule.created",
            payload="{}",
            db_path=db,
        )  # status: pending (non-terminal)
        with pytest.raises(store.NotRedeliverableError):
            dispatcher.redeliver(record["hook_id"], delivery_id)

    def test_redeliver_delivered_row_raises(
        self, db: Path, dispatcher: WebhookDispatcher
    ) -> None:
        _, record = store.create_webhook(URL_HTTPS, actor="test", db_path=db)
        delivery_id = store.insert_delivery(
            record["hook_id"],
            event_id="e1",
            event_type="capsule.created",
            payload="{}",
            db_path=db,
        )
        store.mark_delivery(delivery_id, status="delivered", db_path=db)
        with pytest.raises(store.NotRedeliverableError):
            dispatcher.redeliver(record["hook_id"], delivery_id)

    def test_redeliver_wrong_hook_raises(
        self, db: Path, dispatcher: WebhookDispatcher
    ) -> None:
        _, a = store.create_webhook(URL_HTTPS, actor="test", db_path=db)
        _, b = store.create_webhook(URL_HTTPS, actor="test", db_path=db)
        delivery_id = store.insert_delivery(
            a["hook_id"], event_id="e1", event_type="capsule.created", payload="{}",
            db_path=db,
        )
        store.mark_delivery(delivery_id, status="failed", db_path=db)
        with pytest.raises(store.UnknownDeliveryError):
            dispatcher.redeliver(b["hook_id"], delivery_id)


URL_HTTPS = "https://hooks.example.com/nova"


# ---------------------------------------------------------------------------
# ops.* dedup discipline (ADR-0192 inherited)
# ---------------------------------------------------------------------------


class TestOpsDedup:
    def test_flapping_ops_event_delivers_once_per_window(
        self, db: Path, dispatcher: WebhookDispatcher, receiver: _Receiver
    ) -> None:
        _, record = store.create_webhook(receiver.url, actor="test", db_path=db)
        for _ in range(5):
            dispatcher.enqueue_event(
                LifecycleEvent(
                    type=EventType.OPS_QUOTA_BREACHED,
                    subject=Subject(kind=SubjectKind.OPS, ref="quota"),
                    payload={},
                )
            )
        assert _wait_for(lambda: bool(_rows(db, record["hook_id"], "delivered")))
        time.sleep(0.3)  # allow any (incorrect) extra deliveries to surface
        assert len(_rows(db, record["hook_id"])) == 1


# ---------------------------------------------------------------------------
# End-to-end REST with the app lifespan (dispatcher started by config)
# ---------------------------------------------------------------------------


def _enabled_config(tmp_path: Path, **overrides: Any) -> ServerConfig:
    return ServerConfig(
        db_path=str(tmp_path / "server.db"),
        local_token=TEST_TOKEN,
        webhooks=WebhooksConfig(enabled=True, timeout_s=2.0, **overrides),
    )


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _capsule_zip(run_id: str) -> io.BytesIO:
    manifest = {
        "schema_version": "0.1.0",
        "novafabric_version": "0.6.12",
        "run_id": run_id,
        "created_at": "2026-04-15T10:00:00+00:00",
        "finished_at": "2026-04-15T10:00:01+00:00",
        "duration_ms": 1000,
        "command": ["python", "-c", "print('hi')"],
        "exit_code": 0,
        "status": "success",
        "capture_mode": "cli-wrapper",
        "model_call_count": 0,
        "tool_call_count": 0,
        "mutating_tool_count": 0,
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("capsule.yaml", yaml.safe_dump(manifest))
        zf.writestr("trace.jsonl", "")
    buf.seek(0)
    return buf


class TestRestEndToEnd:
    def test_ping_via_rest_delivers(
        self, tmp_path: Path, receiver: _Receiver
    ) -> None:
        with TestClient(
            create_app(_enabled_config(tmp_path)), raise_server_exceptions=False
        ) as client:
            h = _bearer(TEST_TOKEN)
            created = client.post(
                "/v0/webhooks", json={"url": receiver.url}, headers=h
            ).json()
            hook_id = created["webhook"]["hook_id"]
            resp = client.post(f"/v0/webhooks/{hook_id}/ping", headers=h)
            assert resp.status_code == 202, resp.text
            delivery_id = resp.json()["delivery_id"]

            def _delivered() -> bool:
                rows = client.get(
                    f"/v0/webhooks/{hook_id}/deliveries", headers=h
                ).json()["deliveries"]
                return any(
                    r["delivery_id"] == delivery_id and r["status"] == "delivered"
                    for r in rows
                )

            assert _wait_for(_delivered)
            body = receiver.received[0]["body"]
            header = receiver.received[0]["headers"]["X-NovaFabric-Signature"]
            assert store.verify_delivery_signature(created["secret"], body, header)
            assert (
                client.post(
                    "/v0/webhooks/nosuchid/ping", headers=h
                ).status_code
                == 404
            )

    def test_redeliver_via_rest(self, tmp_path: Path, receiver: _Receiver) -> None:
        receiver.server.response_code = 500  # type: ignore[attr-defined]
        with TestClient(
            create_app(_enabled_config(tmp_path, max_attempts=1)),
            raise_server_exceptions=False,
        ) as client:
            h = _bearer(TEST_TOKEN)
            hook_id = client.post(
                "/v0/webhooks", json={"url": receiver.url}, headers=h
            ).json()["webhook"]["hook_id"]
            delivery_id = client.post(
                f"/v0/webhooks/{hook_id}/ping", headers=h
            ).json()["delivery_id"]

            def _status() -> str:
                rows = client.get(
                    f"/v0/webhooks/{hook_id}/deliveries", headers=h
                ).json()["deliveries"]
                return next(
                    (r["status"] for r in rows if r["delivery_id"] == delivery_id),
                    "?",
                )

            assert _wait_for(lambda: _status() == "failed")
            receiver.server.response_code = 200  # type: ignore[attr-defined]
            resp = client.post(
                f"/v0/webhooks/{hook_id}/deliveries/{delivery_id}/redeliver",
                headers=h,
            )
            assert resp.status_code == 202, resp.text
            assert _wait_for(lambda: _status() == "delivered")

            # Redelivering the now-delivered row is a 409 (not terminal-failed).
            resp = client.post(
                f"/v0/webhooks/{hook_id}/deliveries/{delivery_id}/redeliver",
                headers=h,
            )
            assert resp.status_code == 409
            # Unknown delivery id is a 404.
            resp = client.post(
                f"/v0/webhooks/{hook_id}/deliveries/01NOPE/redeliver", headers=h
            )
            assert resp.status_code == 404

    def test_dead_endpoint_never_delays_capsule_upload(self, tmp_path: Path) -> None:
        """ADR-0205 acceptance: enqueue is non-blocking on the request path."""
        import socket

        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            dead_port = sock.getsockname()[1]  # bound then closed → refused

        from novafabric.server import deps

        app = create_app(_enabled_config(tmp_path))
        capsule_dir = tmp_path / "capsules"
        capsule_dir.mkdir()
        app.dependency_overrides[deps.get_capsule_dir] = lambda: capsule_dir
        with TestClient(app, raise_server_exceptions=False) as client:
            h = _bearer(TEST_TOKEN)
            resp = client.post(
                "/v0/webhooks",
                json={"url": f"http://127.0.0.1:{dead_port}/hook"},
                headers=h,
            )
            assert resp.status_code == 201, resp.text
            hook_id = resp.json()["webhook"]["hook_id"]

            start = time.monotonic()
            upload = client.post(
                "/v0/capsules",
                files={
                    "capsule": (
                        "capsule.zip",
                        _capsule_zip("01TESTWEBHOOK0000000000001"),
                        "application/zip",
                    )
                },
                headers=h,
            )
            elapsed = time.monotonic() - start
            assert upload.status_code == 201, upload.text
            assert elapsed < 2.0  # never blocked by the dead endpoint

            # The delivery row appears and eventually dead-letters — evidence
            # the event was fanned out without touching the request path.
            def _row_exists() -> bool:
                rows = client.get(
                    f"/v0/webhooks/{hook_id}/deliveries", headers=h
                ).json()["deliveries"]
                return any(r["event_type"] == "capsule.created" for r in rows)

            assert _wait_for(_row_exists)

    def test_zero_webhook_rows_upload_unaffected(self, tmp_path: Path) -> None:
        """Contract: with no subscriptions, ingest behaves exactly as before."""
        from novafabric.server import deps

        app = create_app(_enabled_config(tmp_path))
        capsule_dir = tmp_path / "capsules"
        capsule_dir.mkdir()
        app.dependency_overrides[deps.get_capsule_dir] = lambda: capsule_dir
        with TestClient(app, raise_server_exceptions=False) as client:
            upload = client.post(
                "/v0/capsules",
                files={
                    "capsule": (
                        "capsule.zip",
                        _capsule_zip("01TESTWEBHOOK0000000000002"),
                        "application/zip",
                    )
                },
                headers=_bearer(TEST_TOKEN),
            )
            assert upload.status_code == 201, upload.text


class TestEvidencePrecedesState:
    """A terminal delivery state must never be visible before its audit entry.

    The store row is the durable, queryable signal, so every observer synchronises on
    it. If the hash-chained audit append runs *after* the store write, an observer can
    read ``failed`` and find the log does not yet contain the attempt that explains it
    -- and a crash in that window makes the gap permanent. This test asserts the
    ordering directly rather than waiting on the symptom, because the symptom is a race
    and a race that does not fire is not a passing test.
    """

    def test_audit_entry_is_written_before_the_store_row(
        self, db: Path, receiver: _Receiver, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from novafabric.server import webhook_dispatch as wd

        order: list[tuple[str, int]] = []
        real_record = wd.store.record_attempt

        def spy_record(delivery_id, **kw):  # type: ignore[no-untyped-def]
            order.append(("state", len([o for o in order if o[0] == "audit"])))
            return real_record(delivery_id, **kw)

        real_audit = wd.WebhookDispatcher._audit_attempt

        def spy_audit(self, attempt, result, chain_attempt):  # type: ignore[no-untyped-def]
            order.append(("audit", chain_attempt))
            return real_audit(self, attempt, result, chain_attempt)

        monkeypatch.setattr(wd.store, "record_attempt", spy_record)
        monkeypatch.setattr(wd.WebhookDispatcher, "_audit_attempt", spy_audit)

        receiver.server.response_code = 500  # type: ignore[attr-defined]
        d = WebhookDispatcher(
            db_path=db,
            config=DispatchConfig(schedule_s=(0.0, 0.0, 0.0, 0.0, 0.0), timeout_s=2.0),
        )
        d.start()
        try:
            _, record = store.create_webhook(receiver.url, actor="test", db_path=db)
            d.enqueue_event(_event())
            assert _wait_for(lambda: bool(_rows(db, record["hook_id"], "failed")))
        finally:
            d.stop()

        # Every state write must be preceded by at least as many audit appends as
        # there have been state writes -- i.e. they strictly alternate audit-first.
        seen_audit = seen_state = 0
        for kind, _ in order:
            if kind == "audit":
                seen_audit += 1
            else:
                seen_state += 1
                assert seen_audit >= seen_state, (
                    f"state write #{seen_state} landed with only {seen_audit} audit "
                    f"append(s) before it — evidence lags state: {order}"
                )
        assert seen_state == 5, f"expected the 5-attempt schedule, saw {order}"
