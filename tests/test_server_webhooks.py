"""Tests for the webhook subscription registry (ADR-0205 P1, experimental).

Covers:
- secret format `nvwh_<hook_id>_<secret>` and shown-once semantics
- at-rest posture: ADR-0185-wrapped when a backend is configured, documented
  plaintext fallback otherwise; the secret never appears in any API response,
  listing, log line, or audit entry
- URL / event-type / workspace validation with named errors
- the Stripe-style `t=...,v1=...` delivery signature (manual HMAC recompute,
  timestamp tolerance, tamper rejection)
- the `nvwh_` secret-scanner rule (`novafabric-webhook-secret`)
- REST CRUD lifecycle over /v0/webhooks incl. RBAC denials per the spec matrix
"""

from __future__ import annotations

import hashlib
import hmac as hmac_module
import json
import sqlite3
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from novafabric.audit import AuditLog  # noqa: E402
from novafabric.server import webhooks as store  # noqa: E402
from novafabric.server.api_keys import create_key  # noqa: E402
from novafabric.server.app import create_app  # noqa: E402
from novafabric.server.config import ServerConfig  # noqa: E402
from novafabric.trust.novaseal.signing_backend import MockKmsBackend  # noqa: E402

TEST_TOKEN = "test-local-token-adr0205"


@pytest.fixture(autouse=True)
def _tmp_audit_log(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Redirect the hash-chained audit log to a per-test file."""
    path = tmp_path / "audit.jsonl"
    from novafabric.audit import _paths

    monkeypatch.setattr(_paths, "AUDIT_LOG_PATH", path)
    return path


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "registry.db"


def _audit_path(tmp_path: Path) -> Path:
    return tmp_path / "audit.jsonl"


URL = "https://hooks.example.com/nova"


# ---------------------------------------------------------------------------
# Secret format + shown-once + at-rest posture (ADR-0205 D3)
# ---------------------------------------------------------------------------


class TestSecretFormat:
    def test_create_returns_prefixed_secret_and_record(self, db: Path) -> None:
        secret, record = store.create_webhook(URL, actor="test", db_path=db)
        assert secret.startswith(store.SECRET_PREFIX)
        hook_id, secret_part = store.parse_secret(secret)  # type: ignore[misc]
        assert len(hook_id) == 8
        assert len(secret_part) >= 30
        assert record["hook_id"] == hook_id
        assert record["url"] == URL
        assert record["disabled"] is False

    def test_parse_secret_positional(self) -> None:
        assert store.parse_secret("nvwh_ab_cd_ef_SECRETSECRETSECRET") == (
            "ab_cd_ef",
            "SECRETSECRETSECRET",
        )

    def test_parse_secret_rejects_malformed(self) -> None:
        assert store.parse_secret("not-a-secret") is None
        assert store.parse_secret("nvwh_short") is None
        assert store.parse_secret("nvwh_abcdefghXnoseparator") is None
        assert store.parse_secret("nvwh_abcdefgh_") is None


class TestSecretAtRest:
    def test_plaintext_fallback_is_documented_not_hidden(self, db: Path) -> None:
        # No KeyWrappingBackend configured → plaintext in the registry DB, and
        # the posture is surfaced as secret_at_rest="plaintext" (spec).
        secret, record = store.create_webhook(URL, actor="test", db_path=db)
        assert record["secret_at_rest"] == "plaintext"
        assert store.load_secret(record["hook_id"], db_path=db) == secret

    def test_wrapped_at_rest_with_backend(self, db: Path) -> None:
        backend = MockKmsBackend()
        secret, record = store.create_webhook(
            URL, actor="test", db_path=db, wrapping_backend=backend
        )
        assert record["secret_at_rest"] == "wrapped"
        # The raw secret never touches the DB file when wrapped.
        assert secret.encode() not in db.read_bytes()
        # Round-trip through the backend recovers it for HMAC use.
        assert (
            store.load_secret(record["hook_id"], db_path=db, wrapping_backend=backend)
            == secret
        )

    def test_wrapped_secret_unavailable_without_backend(self, db: Path) -> None:
        backend = MockKmsBackend()
        _, record = store.create_webhook(
            URL, actor="test", db_path=db, wrapping_backend=backend
        )
        with pytest.raises(store.SecretUnavailableError):
            store.load_secret(record["hook_id"], db_path=db)

    def test_secret_never_in_listings_or_audit(self, db: Path, tmp_path: Path) -> None:
        secret, record = store.create_webhook(URL, actor="test", db_path=db)
        store.update_webhook(
            record["hook_id"], actor="test", description="d2", db_path=db
        )
        store.delete_webhook(record["hook_id"], actor="test", db_path=db)
        listing = json.dumps(store.list_webhooks(db_path=db))
        assert secret not in listing
        assert "secret_ciphertext" not in listing
        audit_raw = _audit_path(tmp_path).read_text()
        assert secret not in audit_raw

    def test_file_kek_backend_resolves_from_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, db: Path
    ) -> None:
        kek = tmp_path / "webhooks.kek"
        kek.write_bytes(b"\x01" * 32)
        monkeypatch.setenv(store.KEK_PATH_ENV, str(kek))
        backend = store.resolve_wrapping_backend()
        assert backend is not None
        assert backend.kek_ref().startswith("local-kek:")
        secret, record = store.create_webhook(
            URL, actor="test", db_path=db, wrapping_backend=backend
        )
        assert record["secret_at_rest"] == "wrapped"
        assert (
            store.load_secret(record["hook_id"], db_path=db, wrapping_backend=backend)
            == secret
        )

    def test_no_env_means_no_backend(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(store.KEK_PATH_ENV, raising=False)
        assert store.resolve_wrapping_backend() is None


# ---------------------------------------------------------------------------
# Validation (URL / event types / workspace)
# ---------------------------------------------------------------------------


class TestValidation:
    def test_rejects_non_http_url(self, db: Path) -> None:
        for bad in ("ftp://x.example/hook", "not-a-url", "//half", "https://"):
            with pytest.raises(store.InvalidWebhookUrlError):
                store.create_webhook(bad, actor="test", db_path=db)

    def test_rejects_insecure_non_loopback_http(self, db: Path) -> None:
        with pytest.raises(store.InvalidWebhookUrlError):
            store.create_webhook("http://hooks.example.com/x", actor="test", db_path=db)

    def test_allows_http_loopback(self, db: Path) -> None:
        _, record = store.create_webhook(
            "http://127.0.0.1:9999/x", actor="test", db_path=db
        )
        assert record["url"].startswith("http://127.0.0.1")
        _, record2 = store.create_webhook(
            "http://localhost:9999/x", actor="test", db_path=db
        )
        assert record2["url"].startswith("http://localhost")

    def test_allow_insecure_url_opt_out(self, db: Path) -> None:
        _, record = store.create_webhook(
            "http://hooks.example.com/x",
            actor="test",
            db_path=db,
            allow_insecure_url=True,
        )
        assert record["url"] == "http://hooks.example.com/x"

    def test_rejects_private_address_ssrf(self, db: Path) -> None:
        # https so the scheme check passes and the SSRF guard is what fires.
        for internal in (
            "https://10.0.0.5/x",  # private
            "https://192.168.1.1/x",  # private
            "https://169.254.169.254/latest/meta-data",  # link-local (cloud IMDS)
        ):
            with pytest.raises(store.InvalidWebhookUrlError):
                store.create_webhook(internal, actor="test", db_path=db)

    def test_allow_internal_targets_opt_out(self, db: Path) -> None:
        _, record = store.create_webhook(
            "https://10.0.0.5/x",
            actor="test",
            db_path=db,
            allow_internal_targets=True,
        )
        assert record["url"] == "https://10.0.0.5/x"

    def test_loopback_still_allowed_under_ssrf_guard(self, db: Path) -> None:
        # Loopback is a supported first-class target; the SSRF guard must not
        # block it even though allow_internal_targets defaults to False.
        _, record = store.create_webhook(
            "http://127.0.0.1:9999/x", actor="test", db_path=db
        )
        assert record["url"].startswith("http://127.0.0.1")

    def test_rejects_unknown_event_type_listing_valid(self, db: Path) -> None:
        with pytest.raises(store.InvalidEventTypeError) as exc:
            store.create_webhook(
                URL, actor="test", event_types=["no.such.event"], db_path=db
            )
        assert "capsule.created" in str(exc.value)  # valid values listed

    def test_accepts_known_event_types(self, db: Path) -> None:
        _, record = store.create_webhook(
            URL,
            actor="test",
            event_types=["capsule.created", "webhook.ping"],
            db_path=db,
        )
        assert record["event_types"] == ["capsule.created", "webhook.ping"]

    def test_rejects_unknown_workspace(self, db: Path) -> None:
        with pytest.raises(store.UnknownWorkspaceError):
            store.create_webhook(URL, actor="test", workspace="nope", db_path=db)

    def test_accepts_existing_workspace(self, db: Path) -> None:
        from novafabric.server import workspace_store

        workspace_store.ensure_default(db_path=db)
        ws = workspace_store.list_workspaces(db_path=db)[0]
        _, record = store.create_webhook(
            URL, actor="test", workspace=ws["slug"], db_path=db
        )
        assert record["workspace"] == ws["slug"]


# ---------------------------------------------------------------------------
# CRUD + audit trail
# ---------------------------------------------------------------------------


class TestCrud:
    def test_update_fields_and_clear_filter(self, db: Path) -> None:
        _, record = store.create_webhook(
            URL, actor="test", event_types=["capsule.created"], db_path=db
        )
        updated = store.update_webhook(
            record["hook_id"],
            actor="test",
            url="https://hooks.example.com/v2",
            description="new",
            event_types=None,  # explicit clear → all events
            disabled=True,
            db_path=db,
        )
        assert updated["url"] == "https://hooks.example.com/v2"
        assert updated["description"] == "new"
        assert updated["event_types"] is None
        assert updated["disabled"] is True
        assert updated["updated_at"] >= record["updated_at"]

    def test_get_unknown_raises(self, db: Path) -> None:
        with pytest.raises(store.UnknownWebhookError):
            store.get_webhook("nosuchid", db_path=db)

    def test_update_unknown_raises(self, db: Path) -> None:
        with pytest.raises(store.UnknownWebhookError):
            store.update_webhook("nosuchid", actor="test", description="x", db_path=db)

    def test_delete_unknown_raises(self, db: Path) -> None:
        with pytest.raises(store.UnknownWebhookError):
            store.delete_webhook("nosuchid", actor="test", db_path=db)

    def test_lifecycle_is_audited(self, db: Path, tmp_path: Path) -> None:
        _, record = store.create_webhook(URL, actor="admin@x", db_path=db)
        store.update_webhook(
            record["hook_id"], actor="admin@x", disabled=True, db_path=db
        )
        store.delete_webhook(record["hook_id"], actor="admin@x", db_path=db)
        log = AuditLog(_audit_path(tmp_path))
        assert log.verify() == []  # hash chain intact
        types = [
            e.event_type.value for e in log.query(resource_id=record["hook_id"])
        ]
        assert types == ["webhook.create", "webhook.update", "webhook.delete"]


# ---------------------------------------------------------------------------
# Delivery signature (Stripe-style t=...,v1=... over "{t}.{body}")
# ---------------------------------------------------------------------------


class TestDeliverySignature:
    SECRET = "nvwh_abcdefgh_" + "s" * 43
    BODY = b'{"type":"capsule.created","event_id":"01TEST"}'

    def test_signature_matches_manual_hmac(self) -> None:
        t = 1_760_000_000
        header = store.sign_delivery(self.SECRET, self.BODY, t)
        expected = hmac_module.new(
            self.SECRET.encode(),
            f"{t}.".encode() + self.BODY,
            hashlib.sha256,
        ).hexdigest()
        assert header == f"t={t},v1={expected}"

    def test_verify_round_trip(self) -> None:
        t = int(time.time())
        header = store.sign_delivery(self.SECRET, self.BODY, t)
        assert store.verify_delivery_signature(self.SECRET, self.BODY, header)

    def test_verify_rejects_tampered_body(self) -> None:
        t = int(time.time())
        header = store.sign_delivery(self.SECRET, self.BODY, t)
        assert not store.verify_delivery_signature(
            self.SECRET, self.BODY + b"x", header
        )

    def test_verify_rejects_wrong_secret(self) -> None:
        t = int(time.time())
        header = store.sign_delivery(self.SECRET, self.BODY, t)
        assert not store.verify_delivery_signature("nvwh_other_secret", self.BODY, header)

    def test_verify_rejects_timestamp_outside_tolerance(self) -> None:
        t = int(time.time()) - 301  # default tolerance is 300 s
        header = store.sign_delivery(self.SECRET, self.BODY, t)
        assert not store.verify_delivery_signature(self.SECRET, self.BODY, header)
        # ... and a replayed-from-the-future timestamp is equally rejected.
        t_future = int(time.time()) + 301
        header_future = store.sign_delivery(self.SECRET, self.BODY, t_future)
        assert not store.verify_delivery_signature(
            self.SECRET, self.BODY, header_future
        )

    def test_verify_accepts_within_tolerance(self) -> None:
        t = int(time.time()) - 200
        header = store.sign_delivery(self.SECRET, self.BODY, t)
        assert store.verify_delivery_signature(self.SECRET, self.BODY, header)

    def test_verify_rejects_malformed_headers(self) -> None:
        for bad in ("", "t=,v1=", "v1=abc", "t=abc,v1=def", "t=123", "garbage"):
            assert not store.verify_delivery_signature(self.SECRET, self.BODY, bad)


# ---------------------------------------------------------------------------
# Secret-scanner rule (capture/secrets.py) — nvwh_ like nvfk_ (ADR-0205 D3)
# ---------------------------------------------------------------------------


class TestScannerRule:
    def test_nvwh_rule_fires_on_seeded_secret_in_capsule_text(
        self, tmp_path: Path, db: Path
    ) -> None:
        from novafabric.capture.secrets import SecretScannerV0

        secret, _ = store.create_webhook(URL, actor="test", db_path=db)
        capsule_dir = tmp_path / "capsule"
        capsule_dir.mkdir()
        target = capsule_dir / "model-calls.jsonl"
        target.write_text(json.dumps({"messages": [f"my secret is {secret}"]}) + "\n")

        proof = SecretScannerV0(capsule_dir, "run-test").scan_and_redact()

        rule_ids = {f["rule_id"] for f in proof["findings"]}
        assert "novafabric-webhook-secret" in rule_ids
        redacted = target.read_text()
        _, secret_part = store.parse_secret(secret)  # type: ignore[misc]
        assert secret_part not in redacted
        assert "[REDACTED:novafabric-webhook-secret]" in redacted

    def test_redact_secrets_in_text_masks_nvwh(self, db: Path) -> None:
        from novafabric.capture.secrets import redact_secrets_in_text

        secret, _ = store.create_webhook(URL, actor="test", db_path=db)
        out = redact_secrets_in_text(f"leak: {secret}")
        assert secret not in out
        assert "novafabric-webhook-secret" in out


# ---------------------------------------------------------------------------
# REST resource — /v0/webhooks (RBAC per the spec matrix)
# ---------------------------------------------------------------------------


def _client(tmp_path: Path) -> TestClient:
    cfg = ServerConfig(db_path=str(tmp_path / "server.db"), local_token=TEST_TOKEN)
    return TestClient(create_app(cfg), raise_server_exceptions=False)


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class TestRestCrud:
    def test_create_returns_secret_exactly_once(self, tmp_path: Path) -> None:
        client = _client(tmp_path)
        resp = client.post(
            "/v0/webhooks", json={"url": URL}, headers=_bearer(TEST_TOKEN)
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["secret"].startswith("nvwh_")
        assert "shown only once" in body["note"]
        hook_id = body["webhook"]["hook_id"]
        assert body["webhook"]["secret_at_rest"] == "plaintext"

        # Subsequent GET/list never carry the secret (redaction by design).
        got = client.get(f"/v0/webhooks/{hook_id}", headers=_bearer(TEST_TOKEN))
        assert got.status_code == 200
        assert body["secret"] not in got.text
        assert "secret_ciphertext" not in got.text
        listed = client.get("/v0/webhooks", headers=_bearer(TEST_TOKEN))
        assert listed.status_code == 200
        assert body["secret"] not in listed.text

    def test_create_bad_url_and_bad_event_type_and_bad_workspace_400(
        self, tmp_path: Path
    ) -> None:
        client = _client(tmp_path)
        h = _bearer(TEST_TOKEN)
        assert (
            client.post("/v0/webhooks", json={"url": "ftp://x/y"}, headers=h).status_code
            == 400
        )
        assert (
            client.post(
                "/v0/webhooks",
                json={"url": URL, "event_types": ["nope"]},
                headers=h,
            ).status_code
            == 400
        )
        assert (
            client.post(
                "/v0/webhooks",
                json={"url": URL, "workspace": "ghost"},
                headers=h,
            ).status_code
            == 400
        )

    def test_get_unknown_404(self, tmp_path: Path) -> None:
        client = _client(tmp_path)
        resp = client.get("/v0/webhooks/nosuchid", headers=_bearer(TEST_TOKEN))
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "not_found"

    def test_patch_updates_and_secret_is_not_updatable(self, tmp_path: Path) -> None:
        client = _client(tmp_path)
        h = _bearer(TEST_TOKEN)
        hook_id = client.post("/v0/webhooks", json={"url": URL}, headers=h).json()[
            "webhook"
        ]["hook_id"]

        ok = client.patch(
            f"/v0/webhooks/{hook_id}", json={"disabled": True}, headers=h
        )
        assert ok.status_code == 200
        assert ok.json()["webhook"]["disabled"] is True

        denied = client.patch(
            f"/v0/webhooks/{hook_id}", json={"secret": "nvwh_x_y"}, headers=h
        )
        assert denied.status_code == 400
        assert "not updatable" in denied.json()["error"]["message"]

        assert (
            client.patch(
                "/v0/webhooks/nosuchid", json={"disabled": True}, headers=h
            ).status_code
            == 404
        )
        assert (
            client.patch(
                f"/v0/webhooks/{hook_id}", json={"url": "ftp://x/y"}, headers=h
            ).status_code
            == 400
        )

    def test_delete_then_404(self, tmp_path: Path) -> None:
        client = _client(tmp_path)
        h = _bearer(TEST_TOKEN)
        hook_id = client.post("/v0/webhooks", json={"url": URL}, headers=h).json()[
            "webhook"
        ]["hook_id"]
        assert client.delete(f"/v0/webhooks/{hook_id}", headers=h).status_code == 200
        assert client.delete(f"/v0/webhooks/{hook_id}", headers=h).status_code == 404

    def test_deliveries_empty_and_unknown_hook(self, tmp_path: Path) -> None:
        client = _client(tmp_path)
        h = _bearer(TEST_TOKEN)
        hook_id = client.post("/v0/webhooks", json={"url": URL}, headers=h).json()[
            "webhook"
        ]["hook_id"]
        resp = client.get(f"/v0/webhooks/{hook_id}/deliveries", headers=h)
        assert resp.status_code == 200
        assert resp.json() == {"deliveries": [], "next_cursor": None, "total": 0}
        assert (
            client.get("/v0/webhooks/nosuchid/deliveries", headers=h).status_code
            == 404
        )

    def test_ping_and_redeliver_409_while_dispatch_disabled(
        self, tmp_path: Path
    ) -> None:
        # Default config: server.webhooks.enabled=false → no dispatcher.
        client = _client(tmp_path)
        h = _bearer(TEST_TOKEN)
        hook_id = client.post("/v0/webhooks", json={"url": URL}, headers=h).json()[
            "webhook"
        ]["hook_id"]
        resp = client.post(f"/v0/webhooks/{hook_id}/ping", headers=h)
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "webhooks_disabled"
        resp = client.post(
            f"/v0/webhooks/{hook_id}/deliveries/01X/redeliver", headers=h
        )
        assert resp.status_code == 409


class TestRestRbac:
    """Spec matrix: mutations admin-only; list/get/deliveries admin|auditor."""

    def test_unauthenticated_is_401(self, tmp_path: Path) -> None:
        client = _client(tmp_path)
        assert client.get("/v0/webhooks").status_code == 401
        assert client.post("/v0/webhooks", json={"url": URL}).status_code == 401

    def test_auditor_can_read_but_not_mutate(self, tmp_path: Path) -> None:
        client = _client(tmp_path)
        admin = _bearer(TEST_TOKEN)
        auditor_key, _ = create_key("aud@x", ["auditor"], actor="test")
        auditor = _bearer(auditor_key)
        hook_id = client.post("/v0/webhooks", json={"url": URL}, headers=admin).json()[
            "webhook"
        ]["hook_id"]

        assert client.get("/v0/webhooks", headers=auditor).status_code == 200
        assert (
            client.get(f"/v0/webhooks/{hook_id}", headers=auditor).status_code == 200
        )
        assert (
            client.get(
                f"/v0/webhooks/{hook_id}/deliveries", headers=auditor
            ).status_code
            == 200
        )
        # Mutations are admin-only (403 for auditor).
        assert (
            client.post("/v0/webhooks", json={"url": URL}, headers=auditor).status_code
            == 403
        )
        assert (
            client.patch(
                f"/v0/webhooks/{hook_id}", json={"disabled": True}, headers=auditor
            ).status_code
            == 403
        )
        assert (
            client.delete(f"/v0/webhooks/{hook_id}", headers=auditor).status_code
            == 403
        )
        assert (
            client.post(f"/v0/webhooks/{hook_id}/ping", headers=auditor).status_code
            == 403
        )
        assert (
            client.post(
                f"/v0/webhooks/{hook_id}/deliveries/01X/redeliver", headers=auditor
            ).status_code
            == 403
        )

    def test_reader_and_writer_are_denied_everything(self, tmp_path: Path) -> None:
        client = _client(tmp_path)
        for roles in (["reader"], ["writer"]):
            key, _ = create_key(f"{roles[0]}@x", roles, actor="test")
            h = _bearer(key)
            assert client.get("/v0/webhooks", headers=h).status_code == 403
            assert (
                client.post("/v0/webhooks", json={"url": URL}, headers=h).status_code
                == 403
            )


# ---------------------------------------------------------------------------
# Delivery-log persistence primitives (payload cap, retention pruning)
# ---------------------------------------------------------------------------


class TestDeliveryLogPrimitives:
    def _hook(self, db: Path) -> str:
        _, record = store.create_webhook(URL, actor="test", db_path=db)
        return record["hook_id"]

    def test_payload_cap_truncates_with_marker(self, db: Path) -> None:
        hook_id = self._hook(db)
        big = "x" * (store.PAYLOAD_CAP_BYTES + 100)
        delivery_id = store.insert_delivery(
            hook_id, event_id="e1", event_type="capsule.created", payload=big,
            db_path=db,
        )
        row = store.get_delivery(delivery_id, db_path=db)
        assert len(row["payload"].encode()) <= store.PAYLOAD_CAP_BYTES
        assert row["last_error"] == "payload_truncated"

    def test_row_cap_prunes_oldest_terminal_rows(self, db: Path) -> None:
        hook_id = self._hook(db)
        ids = []
        for i in range(7):
            delivery_id = store.insert_delivery(
                hook_id,
                event_id=f"e{i}",
                event_type="capsule.created",
                payload="{}",
                retention_rows=5,
                db_path=db,
            )
            store.mark_delivery(delivery_id, status="delivered", db_path=db)
            ids.append(delivery_id)
        rows = store.list_deliveries(hook_id, db_path=db)
        assert len(rows) == 5
        surviving = {r["delivery_id"] for r in rows}
        assert ids[0] not in surviving  # oldest pruned first
        assert ids[-1] in surviving

    def test_age_cap_prunes_old_terminal_rows_only(self, db: Path) -> None:
        hook_id = self._hook(db)
        old_terminal = store.insert_delivery(
            hook_id, event_id="old-t", event_type="capsule.created", payload="{}",
            db_path=db,
        )
        store.mark_delivery(old_terminal, status="failed", db_path=db)
        old_pending = store.insert_delivery(
            hook_id, event_id="old-p", event_type="capsule.created", payload="{}",
            db_path=db,
        )
        # Backdate both far beyond the 30-day age cap.
        conn = sqlite3.connect(str(db))
        conn.execute(
            "UPDATE webhook_deliveries SET created_at = '2020-01-01T00:00:00+00:00'"
        )
        conn.commit()
        conn.close()
        store.prune_deliveries(hook_id, db_path=db)
        surviving = {r["delivery_id"] for r in store.list_deliveries(hook_id, db_path=db)}
        assert old_terminal not in surviving  # aged-out terminal row pruned
        assert old_pending in surviving  # non-terminal rows are never age-pruned

    def test_status_filter_and_order(self, db: Path) -> None:
        hook_id = self._hook(db)
        d1 = store.insert_delivery(
            hook_id, event_id="e1", event_type="capsule.created", payload="{}",
            db_path=db,
        )
        d2 = store.insert_delivery(
            hook_id, event_id="e2", event_type="capsule.created", payload="{}",
            db_path=db,
        )
        store.mark_delivery(d1, status="failed", db_path=db)
        failed = store.list_deliveries(hook_id, status="failed", db_path=db)
        assert [r["delivery_id"] for r in failed] == [d1]
        all_rows = store.list_deliveries(hook_id, db_path=db)
        assert {r["delivery_id"] for r in all_rows} == {d1, d2}
