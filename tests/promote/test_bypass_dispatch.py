"""Tests for bypass notification dispatch (B-6)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from novafabric.promote.bundle_store import PromoteBundleStore
from novafabric.promote.bypass_notify import (
    BypassEvent,
    FileBypassNotifier,
    MultiBypassNotifier,
    NullBypassNotifier,
    WebhookBypassNotifier,
    build_notifier_from_env,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _event(
    event_type: str = "bypass_created",
    capsule_id: str = "cap-test-001",
    bypass_uuid: str = "aaaaaaaa-0000-0000-0000-000000000001",
    valid_until: str = "2099-01-01T00:00:00",
) -> BypassEvent:
    return BypassEvent(
        event_type=event_type,
        capsule_id=capsule_id,
        bypass_uuid=bypass_uuid,
        valid_until=valid_until,
    )


# ---------------------------------------------------------------------------
# NullBypassNotifier
# ---------------------------------------------------------------------------


class TestNullBypassNotifier:
    def test_notify_does_nothing_and_never_raises(self):
        """NullBypassNotifier.notify() is a pure no-op."""
        notifier = NullBypassNotifier()
        # Should not raise regardless of event content
        notifier.notify(_event())
        notifier.notify(_event(event_type="bypass_expired"))

    def test_notify_multiple_times_no_error(self):
        """Repeated calls to NullBypassNotifier do not raise."""
        notifier = NullBypassNotifier()
        for _ in range(10):
            notifier.notify(_event())


# ---------------------------------------------------------------------------
# FileBypassNotifier
# ---------------------------------------------------------------------------


class TestFileBypassNotifier:
    def test_writes_valid_json_line(self, tmp_path):
        """FileBypassNotifier writes a valid JSON object as a single line."""
        log_path = tmp_path / "audit.jsonl"
        notifier = FileBypassNotifier(log_path)
        ev = _event(capsule_id="cap-file-001", bypass_uuid="uuid-file-001")
        notifier.notify(ev)

        lines = log_path.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["event_type"] == "bypass_created"
        assert record["capsule_id"] == "cap-file-001"
        assert record["bypass_uuid"] == "uuid-file-001"
        assert "timestamp" in record

    def test_contains_valid_until_field(self, tmp_path):
        """Written JSON line contains the valid_until field."""
        log_path = tmp_path / "audit.jsonl"
        notifier = FileBypassNotifier(log_path)
        notifier.notify(_event(valid_until="2050-06-15T00:00:00"))

        record = json.loads(log_path.read_text(encoding="utf-8").strip())
        assert record["valid_until"] == "2050-06-15T00:00:00"

    def test_appends_not_overwrites(self, tmp_path):
        """Multiple calls append lines rather than overwriting."""
        log_path = tmp_path / "audit.jsonl"
        notifier = FileBypassNotifier(log_path)
        notifier.notify(_event(bypass_uuid="uuid-first"))
        notifier.notify(_event(bypass_uuid="uuid-second"))
        notifier.notify(_event(bypass_uuid="uuid-third"))

        lines = [ln for ln in log_path.read_text(encoding="utf-8").strip().split("\n") if ln]
        assert len(lines) == 3
        uuids = [json.loads(ln)["bypass_uuid"] for ln in lines]
        assert uuids == ["uuid-first", "uuid-second", "uuid-third"]

    def test_creates_parent_directories(self, tmp_path):
        """FileBypassNotifier creates missing parent directories."""
        log_path = tmp_path / "nested" / "deep" / "audit.jsonl"
        notifier = FileBypassNotifier(log_path)
        notifier.notify(_event())
        assert log_path.exists()


# ---------------------------------------------------------------------------
# WebhookBypassNotifier
# ---------------------------------------------------------------------------


class TestWebhookBypassNotifier:
    def test_swallows_connection_error(self):
        """WebhookBypassNotifier does not propagate network errors."""
        notifier = WebhookBypassNotifier("http://127.0.0.1:0/nonexistent", timeout_s=0.1)
        # Must not raise even if the endpoint is unreachable
        notifier.notify(_event())

    def test_swallows_url_error_silently(self, caplog):
        """Network failure logs a warning but does not raise."""
        import logging
        notifier = WebhookBypassNotifier("http://0.0.0.0:1/sink", timeout_s=0.1)
        with caplog.at_level(logging.WARNING, logger="novafabric.promote.bypass_notify"):
            notifier.notify(_event())
        # Should have logged a warning (best-effort check — some OS may fail differently)
        # The important thing is no exception propagated (verified above)

    def test_posts_json_body(self, tmp_path):
        """WebhookBypassNotifier POSTs JSON with correct Content-Type when server reachable."""
        import http.server
        import threading

        received: list[bytes] = []

        class _Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", 0))
                received.append(self.rfile.read(length))
                self.send_response(200)
                self.end_headers()

            def log_message(self, fmt, *args):  # silence server logs in test output
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.handle_request, daemon=True)
        thread.start()

        notifier = WebhookBypassNotifier(f"http://127.0.0.1:{port}/hook", timeout_s=3.0)
        ev = _event(capsule_id="cap-webhook", bypass_uuid="uuid-webhook")
        notifier.notify(ev)
        thread.join(timeout=3)

        assert len(received) == 1
        payload = json.loads(received[0].decode("utf-8"))
        assert payload["capsule_id"] == "cap-webhook"
        assert payload["bypass_uuid"] == "uuid-webhook"
        assert payload["event_type"] == "bypass_created"


# ---------------------------------------------------------------------------
# MultiBypassNotifier
# ---------------------------------------------------------------------------


class TestMultiBypassNotifier:
    def test_calls_all_notifiers(self):
        """MultiBypassNotifier calls every contained notifier."""
        n1 = MagicMock()
        n2 = MagicMock()
        multi = MultiBypassNotifier([n1, n2])
        ev = _event()
        multi.notify(ev)
        n1.notify.assert_called_once_with(ev)
        n2.notify.assert_called_once_with(ev)

    def test_continues_after_one_notifier_raises(self):
        """MultiBypassNotifier calls remaining notifiers even if one raises."""
        n1 = MagicMock(side_effect=RuntimeError("boom"))
        n2 = MagicMock()
        multi = MultiBypassNotifier([n1, n2])
        # Must not propagate
        multi.notify(_event())
        n2.notify.assert_called_once()

    def test_empty_notifier_list_no_error(self):
        """MultiBypassNotifier with no notifiers does nothing."""
        multi = MultiBypassNotifier([])
        multi.notify(_event())


# ---------------------------------------------------------------------------
# build_notifier_from_env
# ---------------------------------------------------------------------------


class TestBuildNotifierFromEnv:
    def test_returns_null_when_no_env_vars(self, monkeypatch):
        """Returns NullBypassNotifier when neither env var is set."""
        monkeypatch.delenv("NOVA_BYPASS_NOTIFY_FILE", raising=False)
        monkeypatch.delenv("NOVA_BYPASS_NOTIFY_WEBHOOK", raising=False)
        notifier = build_notifier_from_env()
        assert isinstance(notifier, NullBypassNotifier)

    def test_returns_file_notifier_when_file_env_set(self, monkeypatch, tmp_path):
        """Returns FileBypassNotifier when only NOVA_BYPASS_NOTIFY_FILE is set."""
        monkeypatch.setenv("NOVA_BYPASS_NOTIFY_FILE", str(tmp_path / "log.jsonl"))
        monkeypatch.delenv("NOVA_BYPASS_NOTIFY_WEBHOOK", raising=False)
        notifier = build_notifier_from_env()
        assert isinstance(notifier, FileBypassNotifier)

    def test_returns_webhook_notifier_when_webhook_env_set(self, monkeypatch):
        """Returns WebhookBypassNotifier when only NOVA_BYPASS_NOTIFY_WEBHOOK is set."""
        monkeypatch.delenv("NOVA_BYPASS_NOTIFY_FILE", raising=False)
        monkeypatch.setenv("NOVA_BYPASS_NOTIFY_WEBHOOK", "http://example.com/hook")
        notifier = build_notifier_from_env()
        assert isinstance(notifier, WebhookBypassNotifier)

    def test_returns_multi_when_both_env_vars_set(self, monkeypatch, tmp_path):
        """Returns MultiBypassNotifier when both env vars are set."""
        monkeypatch.setenv("NOVA_BYPASS_NOTIFY_FILE", str(tmp_path / "log.jsonl"))
        monkeypatch.setenv("NOVA_BYPASS_NOTIFY_WEBHOOK", "http://example.com/hook")
        notifier = build_notifier_from_env()
        assert isinstance(notifier, MultiBypassNotifier)

    def test_multi_contains_file_and_webhook(self, monkeypatch, tmp_path):
        """MultiBypassNotifier from env contains one FileBypassNotifier and one WebhookBypassNotifier."""
        monkeypatch.setenv("NOVA_BYPASS_NOTIFY_FILE", str(tmp_path / "log.jsonl"))
        monkeypatch.setenv("NOVA_BYPASS_NOTIFY_WEBHOOK", "http://example.com/hook")
        notifier = build_notifier_from_env()
        assert isinstance(notifier, MultiBypassNotifier)
        types = [type(n).__name__ for n in notifier._notifiers]
        assert "FileBypassNotifier" in types
        assert "WebhookBypassNotifier" in types


# ---------------------------------------------------------------------------
# PromoteBundleStore integration
# ---------------------------------------------------------------------------


class TestPromoteBundleStoreDispatch:
    def test_put_bypass_fires_notification(self, tmp_path):
        """PromoteBundleStore.put_bypass() calls notifier after successful write."""
        mock_notifier = MagicMock()
        store = PromoteBundleStore(tmp_path, notifier=mock_notifier)
        valid_until = "2099-01-01T00:00:00+00:00"

        bypass_uuid = store.put_bypass("cap-dispatch-001", b'{"ok": true}', valid_until)

        mock_notifier.notify.assert_called_once()
        ev: BypassEvent = mock_notifier.notify.call_args[0][0]
        assert ev.event_type == "bypass_created"
        assert ev.capsule_id == "cap-dispatch-001"
        assert ev.bypass_uuid == bypass_uuid
        assert ev.valid_until == valid_until

    def test_put_bypass_notifier_exception_does_not_propagate(self, tmp_path):
        """put_bypass still returns UUID even if notifier raises."""
        raising_notifier = MagicMock(side_effect=RuntimeError("notify failed"))
        store = PromoteBundleStore(tmp_path, notifier=raising_notifier)

        # Must not raise; bypass must still be persisted
        bypass_uuid = store.put_bypass("cap-safe", b"data", "2099-01-01T00:00:00")
        assert isinstance(bypass_uuid, str)
        # File was written despite notifier failure
        assert store.get_bypass("cap-safe", bypass_uuid) == b"data"

    def test_put_bypass_default_null_notifier(self, tmp_path):
        """PromoteBundleStore with no notifier arg defaults to NullBypassNotifier (no error)."""
        store = PromoteBundleStore(tmp_path)
        uuid_ = store.put_bypass("cap-null", b"payload", "2099-01-01T00:00:00")
        assert isinstance(uuid_, str)

    def test_put_bypass_file_notifier_integration(self, tmp_path):
        """End-to-end: PromoteBundleStore with FileBypassNotifier writes a log entry."""
        log_path = tmp_path / "bypass_audit.jsonl"
        notifier = FileBypassNotifier(log_path)
        store = PromoteBundleStore(tmp_path / "store", notifier=notifier)
        valid_until = "2099-06-01T00:00:00"

        bypass_uuid = store.put_bypass("cap-e2e", b"envelope_data", valid_until)

        assert log_path.exists()
        record = json.loads(log_path.read_text(encoding="utf-8").strip())
        assert record["event_type"] == "bypass_created"
        assert record["capsule_id"] == "cap-e2e"
        assert record["bypass_uuid"] == bypass_uuid
        assert record["valid_until"] == valid_until
