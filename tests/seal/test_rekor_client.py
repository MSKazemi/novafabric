"""Tests for the optional Rekor transparency log client."""

from __future__ import annotations

import base64
import hashlib
import json
from unittest.mock import MagicMock, patch

from novafabric.promote.rekor_client import maybe_publish


def _make_envelope(payload_b64: str | None = None) -> bytes:
    """Build a minimal DSSE envelope for testing."""
    if payload_b64 is None:
        payload_b64 = base64.urlsafe_b64encode(b'{"test": true}').rstrip(b"=").decode()
    return json.dumps(
        {
            "payload": payload_b64,
            "payloadType": "application/vnd.novafabric.promote.bypass+json",
            "signatures": [{"keyid": "abc", "sig": "xyz", "cert": "pem"}],
        }
    ).encode()


class TestMaybePublishNoUrl:
    def test_no_rekor_url_returns_none(self, monkeypatch):
        """Without NOVA_REKOR_URL, maybe_publish returns None."""
        monkeypatch.delenv("NOVA_REKOR_URL", raising=False)
        result = maybe_publish(_make_envelope())
        assert result is None


class TestMaybePublishSuccess:
    def test_rekor_publish_success(self, monkeypatch):
        """Mock httpx.post returning {uuid: {...}}; maybe_publish returns the UUID."""
        monkeypatch.setenv("NOVA_REKOR_URL", "http://localhost:3000")

        mock_resp = MagicMock()
        mock_resp.status_code = 201
        log_uuid = "abc123def456"
        mock_resp.json.return_value = {log_uuid: {"body": "..."}}

        with patch("httpx.post", return_value=mock_resp) as mock_post:
            result = maybe_publish(_make_envelope())

        assert result == log_uuid
        mock_post.assert_called_once()

    def test_rekor_publish_200_status(self, monkeypatch):
        """HTTP 200 is also accepted as success."""
        monkeypatch.setenv("NOVA_REKOR_URL", "http://localhost:3000")

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        log_uuid = "uuid-200-ok"
        mock_resp.json.return_value = {log_uuid: {}}

        with patch("httpx.post", return_value=mock_resp):
            result = maybe_publish(_make_envelope())

        assert result == log_uuid


class TestMaybePublishErrors:
    def test_rekor_publish_http_error(self, monkeypatch):
        """HTTP 500 response returns None without raising."""
        monkeypatch.setenv("NOVA_REKOR_URL", "http://localhost:3000")

        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "Internal Server Error"

        with patch("httpx.post", return_value=mock_resp):
            result = maybe_publish(_make_envelope())

        assert result is None

    def test_rekor_publish_network_error(self, monkeypatch):
        """Network exception returns None without raising."""
        monkeypatch.setenv("NOVA_REKOR_URL", "http://localhost:3000")

        import httpx

        with patch("httpx.post", side_effect=httpx.ConnectError("refused")):
            result = maybe_publish(_make_envelope())

        assert result is None

    def test_rekor_publish_http_404(self, monkeypatch):
        """HTTP 404 returns None without raising."""
        monkeypatch.setenv("NOVA_REKOR_URL", "http://localhost:3000")

        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_resp.text = "not found"

        with patch("httpx.post", return_value=mock_resp):
            result = maybe_publish(_make_envelope())

        assert result is None


class TestMaybePublishPayloadHash:
    def test_rekor_publish_parses_envelope(self, monkeypatch):
        """Checks the POST body contains correct payloadHash for the envelope payload."""
        monkeypatch.setenv("NOVA_REKOR_URL", "http://localhost:3000")

        raw_payload = b'{"capsule_id": "test-cap"}'
        payload_b64 = base64.urlsafe_b64encode(raw_payload).rstrip(b"=").decode()
        envelope = _make_envelope(payload_b64)
        expected_hash = hashlib.sha256(raw_payload).hexdigest()

        mock_resp = MagicMock()
        mock_resp.status_code = 201
        mock_resp.json.return_value = {"uuid-xyz": {}}

        with patch("httpx.post", return_value=mock_resp) as mock_post:
            maybe_publish(envelope)

        call_kwargs = mock_post.call_args
        sent_body = call_kwargs.kwargs.get("json") or call_kwargs.args[1] if len(call_kwargs.args) > 1 else call_kwargs.kwargs["json"]
        assert sent_body["spec"]["payloadHash"]["value"] == expected_hash
        assert sent_body["spec"]["payloadHash"]["algorithm"] == "sha256"
        assert sent_body["kind"] == "dsse"
        assert sent_body["apiVersion"] == "0.0.1"
