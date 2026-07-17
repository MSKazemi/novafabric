"""Tests for first-class API keys (ADR-0193 Track 1 first slice, experimental).

Covers:
- key format `nvfk_<key_id>_<secret>` and shown-once semantics
- hash-only storage: the secret never appears in the DB file, logs, or audit log
- constant-time verification (hmac.compare_digest on the sha256 hex digests)
- revoked / expired keys are rejected immediately
- role scoping flows through the existing RBAC route checks
- the `nvfk_` secret-scanner rule fires on a seeded key in capsule text
- CLI smoke: create prints once, list hides secrets, revoke works
"""

from __future__ import annotations

import hashlib
import hmac as hmac_module
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from novafabric.audit import AuditLog  # noqa: E402
from novafabric.server.api_keys import (  # noqa: E402
    KEY_PREFIX,
    InvalidRoleError,
    RevokedKeyError,
    create_key,
    key_status,
    list_keys,
    parse_key,
    revoke_key,
    rotate_key,
    verify_key,
)
from novafabric.server.app import create_app  # noqa: E402
from novafabric.server.config import ServerConfig  # noqa: E402

TEST_TOKEN = "test-local-token-adr0193"


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


# ---------------------------------------------------------------------------
# Format + shown-once
# ---------------------------------------------------------------------------


class TestKeyFormat:
    def test_create_returns_prefixed_key(self, db: Path) -> None:
        key, record = create_key("alice@example.com", ["reader"], actor="test", db_path=db)
        assert key.startswith(KEY_PREFIX)
        key_id, secret = parse_key(key)  # type: ignore[misc]
        assert len(key_id) == 8
        assert len(secret) >= 30
        assert record["key_id"] == key_id

    def test_parse_key_positional(self) -> None:
        # key_id may itself contain '_' (urlsafe alphabet) — parsing is positional.
        assert parse_key("nvfk_ab_cd_ef_SECRETSECRETSECRET") == ("ab_cd_ef", "SECRETSECRETSECRET")

    def test_parse_key_rejects_malformed(self) -> None:
        assert parse_key("not-a-key") is None
        assert parse_key("nvfk_short") is None
        assert parse_key("nvfk_abcdefghXnosecondseparator") is None
        assert parse_key("nvfk_abcdefgh_") is None

    def test_create_rejects_unknown_role(self, db: Path) -> None:
        with pytest.raises(InvalidRoleError):
            create_key("alice@example.com", ["root"], actor="test", db_path=db)

    def test_create_rejects_empty_roles(self, db: Path) -> None:
        with pytest.raises(InvalidRoleError):
            create_key("alice@example.com", [], actor="test", db_path=db)


# ---------------------------------------------------------------------------
# Hash-only storage: secret never persisted or logged
# ---------------------------------------------------------------------------


class TestSecretNeverPersisted:
    def test_secret_absent_from_db_bytes(self, db: Path, tmp_path: Path) -> None:
        key, _ = create_key("alice@example.com", ["reader"], actor="test", db_path=db)
        _, secret = parse_key(key)  # type: ignore[misc]
        raw = db.read_bytes()
        assert secret.encode() not in raw
        assert key.encode() not in raw
        # ... but the sha256 of the secret IS stored (hash-only storage).
        digest = hashlib.sha256(secret.encode()).hexdigest()
        rows = sqlite3.connect(str(db)).execute(
            "SELECT secret_sha256 FROM api_keys"
        ).fetchall()
        assert [(digest,)] == rows

    def test_secret_absent_from_logs(
        self, db: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        with caplog.at_level(logging.DEBUG):
            key, _ = create_key("alice@example.com", ["reader"], actor="test", db_path=db)
            verify_key(key, db_path=db)
        _, secret = parse_key(key)  # type: ignore[misc]
        assert secret not in caplog.text
        assert key not in caplog.text

    def test_secret_absent_from_audit_log(self, db: Path, tmp_path: Path) -> None:
        key, record = create_key("alice@example.com", ["reader"], actor="test", db_path=db)
        revoke_key(record["key_id"], actor="test", db_path=db)
        _, secret = parse_key(key)  # type: ignore[misc]
        audit_raw = _audit_path(tmp_path).read_text()
        assert secret not in audit_raw
        digest = hashlib.sha256(secret.encode()).hexdigest()
        assert digest not in audit_raw  # never hashes either (ADR-0193 D5)


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


class TestVerifyKey:
    def test_valid_key_returns_principal_and_roles(self, db: Path) -> None:
        key, _ = create_key(
            "svc:ci-bot", ["reader", "writer"], actor="test", db_path=db
        )
        ctx = verify_key(key, db_path=db)
        assert ctx is not None
        assert ctx.subject == "svc:ci-bot"
        assert sorted(ctx.roles) == ["reader", "writer"]

    def test_wrong_secret_rejected(self, db: Path) -> None:
        key, record = create_key("alice@example.com", ["reader"], actor="test", db_path=db)
        forged = f"nvfk_{record['key_id']}_{'x' * 43}"
        assert verify_key(forged, db_path=db) is None

    def test_unknown_key_id_rejected(self, db: Path) -> None:
        create_key("alice@example.com", ["reader"], actor="test", db_path=db)
        assert verify_key(f"nvfk_zzzzzzzz_{'x' * 43}", db_path=db) is None

    def test_malformed_key_rejected(self, db: Path) -> None:
        assert verify_key("nvfk_garbage", db_path=db) is None
        assert verify_key("", db_path=db) is None

    def test_verify_uses_constant_time_compare(self, db: Path) -> None:
        key, _ = create_key("alice@example.com", ["reader"], actor="test", db_path=db)
        with patch(
            "novafabric.server.api_keys.hmac.compare_digest",
            wraps=hmac_module.compare_digest,
        ) as cmp:
            assert verify_key(key, db_path=db) is not None
        assert cmp.called

    def test_revoked_key_rejected_immediately(self, db: Path) -> None:
        key, record = create_key("alice@example.com", ["reader"], actor="test", db_path=db)
        assert verify_key(key, db_path=db) is not None
        revoke_key(record["key_id"], actor="test", db_path=db)
        assert verify_key(key, db_path=db) is None

    def test_expired_key_rejected(self, db: Path) -> None:
        key, record = create_key(
            "alice@example.com", ["reader"], actor="test", db_path=db,
            expires_in_days=30,
        )
        # Backdate the expiry directly in the store.
        past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        conn = sqlite3.connect(str(db))
        conn.execute(
            "UPDATE api_keys SET expires_at = ? WHERE key_id = ?",
            (past, record["key_id"]),
        )
        conn.commit()
        conn.close()
        assert verify_key(key, db_path=db) is None

    def test_revoke_unknown_key_raises(self, db: Path) -> None:
        with pytest.raises(KeyError):
            revoke_key("nosuchid", actor="test", db_path=db)


# ---------------------------------------------------------------------------
# list_keys — never secrets, never hashes
# ---------------------------------------------------------------------------


class TestListKeys:
    def test_list_returns_metadata_without_secrets(self, db: Path) -> None:
        key, record = create_key(
            "alice@example.com",
            ["reader"],
            actor="test",
            db_path=db,
            workspace="ml-team",
        )
        _, secret = parse_key(key)  # type: ignore[misc]
        rows = list_keys(db_path=db)
        assert len(rows) == 1
        row = rows[0]
        assert row["key_id"] == record["key_id"]
        assert row["owner"] == "alice@example.com"
        assert row["roles"] == ["reader"]
        assert row["workspace"] == "ml-team"
        assert row["revoked_at"] is None
        dumped = json.dumps(rows)
        assert "secret_sha256" not in dumped
        assert secret not in dumped
        assert hashlib.sha256(secret.encode()).hexdigest() not in dumped


# ---------------------------------------------------------------------------
# Audit trail (ADR-0193 D5)
# ---------------------------------------------------------------------------


class TestAudit:
    def test_create_and_revoke_are_audited(self, db: Path, tmp_path: Path) -> None:
        _, record = create_key(
            "alice@example.com", ["reader"], actor="admin@example.com", db_path=db
        )
        revoke_key(record["key_id"], actor="admin@example.com", db_path=db)

        log = AuditLog(_audit_path(tmp_path))
        assert log.verify() == []  # chain intact
        entries = log.query(resource_id=record["key_id"])
        types = [e.event_type.value for e in entries]
        assert types == ["api_key.create", "api_key.revoke"]
        assert all(e.actor == "admin@example.com" for e in entries)


# ---------------------------------------------------------------------------
# Rotation (ADR-0193 D3, slice 2)
# ---------------------------------------------------------------------------


class TestRotate:
    def test_rotate_creates_valid_successor_with_identical_bindings(
        self, db: Path
    ) -> None:
        _, record = create_key(
            "svc:ci-bot",
            ["reader", "writer"],
            actor="test",
            db_path=db,
            workspace="ml-team",
            expires_in_days=30,
        )
        successor, succ_record = rotate_key(
            record["key_id"], actor="test", db_path=db
        )
        # successor is a brand-new, distinct, immediately-valid key
        assert succ_record["key_id"] != record["key_id"]
        ctx = verify_key(successor, db_path=db)
        assert ctx is not None
        assert ctx.subject == "svc:ci-bot"
        assert sorted(ctx.roles) == ["reader", "writer"]
        # identical bindings copied from the predecessor
        assert succ_record["owner"] == "svc:ci-bot"
        assert succ_record["roles"] == ["reader", "writer"]
        assert succ_record["workspace"] == "ml-team"
        assert succ_record["expires_at"] is not None

    def test_predecessor_valid_during_overlap_window(self, db: Path) -> None:
        key, record = create_key("alice@x", ["reader"], actor="test", db_path=db)
        assert verify_key(key, db_path=db) is not None
        rotate_key(record["key_id"], actor="test", db_path=db, overlap_seconds=3600)
        # Both old and new are valid inside the window.
        assert verify_key(key, db_path=db) is not None

    def test_predecessor_rejected_after_window_zero_overlap(self, db: Path) -> None:
        key, record = create_key("alice@x", ["reader"], actor="test", db_path=db)
        rotate_key(record["key_id"], actor="test", db_path=db, overlap_seconds=0)
        # Window already elapsed → predecessor rejected at verify time.
        assert verify_key(key, db_path=db) is None

    def test_predecessor_rejected_after_window_backdated(self, db: Path) -> None:
        key, record = create_key("alice@x", ["reader"], actor="test", db_path=db)
        rotate_key(record["key_id"], actor="test", db_path=db, overlap_seconds=3600)
        past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        conn = sqlite3.connect(str(db))
        conn.execute(
            "UPDATE api_keys SET rotate_expires_at = ? WHERE key_id = ?",
            (past, record["key_id"]),
        )
        conn.commit()
        conn.close()
        assert verify_key(key, db_path=db) is None

    def test_rotate_is_audited_on_both_keys(self, db: Path, tmp_path: Path) -> None:
        _, record = create_key(
            "alice@x", ["reader"], actor="admin@x", db_path=db
        )
        _, succ = rotate_key(record["key_id"], actor="admin@x", db_path=db)

        log = AuditLog(_audit_path(tmp_path))
        assert log.verify() == []
        pred_types = [
            e.event_type.value for e in log.query(resource_id=record["key_id"])
        ]
        assert "api_key.rotate" in pred_types
        succ_types = [
            e.event_type.value for e in log.query(resource_id=succ["key_id"])
        ]
        assert "api_key.create" in succ_types

    def test_rotate_records_never_leak_secret(
        self, db: Path, tmp_path: Path
    ) -> None:
        _, record = create_key("alice@x", ["reader"], actor="test", db_path=db)
        successor, _ = rotate_key(record["key_id"], actor="test", db_path=db)
        _, secret = parse_key(successor)  # type: ignore[misc]
        audit_raw = _audit_path(tmp_path).read_text()
        assert secret not in audit_raw
        assert hashlib.sha256(secret.encode()).hexdigest() not in audit_raw

    def test_rotate_unknown_key_raises(self, db: Path) -> None:
        with pytest.raises(KeyError):
            rotate_key("nosuchid", actor="test", db_path=db)

    def test_rotate_revoked_key_raises(self, db: Path) -> None:
        _, record = create_key("alice@x", ["reader"], actor="test", db_path=db)
        revoke_key(record["key_id"], actor="test", db_path=db)
        with pytest.raises(RevokedKeyError):
            rotate_key(record["key_id"], actor="test", db_path=db)

    def test_rotated_predecessor_status_is_revoked_after_window(
        self, db: Path
    ) -> None:
        _, record = create_key("alice@x", ["reader"], actor="test", db_path=db)
        rotate_key(record["key_id"], actor="test", db_path=db, overlap_seconds=0)
        rows = {r["key_id"]: r for r in list_keys(db_path=db)}
        assert key_status(rows[record["key_id"]]) == "revoked"


# ---------------------------------------------------------------------------
# Coarse last_used_at tracking (ADR-0193 D4, slice 2)
# ---------------------------------------------------------------------------


class TestLastUsed:
    def _last_used(self, db: Path, key_id: str) -> str | None:
        return {r["key_id"]: r for r in list_keys(db_path=db)}[key_id]["last_used_at"]

    def test_last_used_set_on_first_verify(self, db: Path) -> None:
        key, record = create_key("alice@x", ["reader"], actor="test", db_path=db)
        assert self._last_used(db, record["key_id"]) is None
        assert verify_key(key, db_path=db) is not None
        assert self._last_used(db, record["key_id"]) is not None

    def test_last_used_not_updated_within_interval(self, db: Path) -> None:
        key, record = create_key("alice@x", ["reader"], actor="test", db_path=db)
        verify_key(key, db_path=db, lastused_interval_seconds=86400)
        first = self._last_used(db, record["key_id"])
        # A second verify inside the interval must not rewrite last_used_at.
        verify_key(key, db_path=db, lastused_interval_seconds=86400)
        assert self._last_used(db, record["key_id"]) == first

    def test_last_used_updates_after_interval(self, db: Path) -> None:
        key, record = create_key("alice@x", ["reader"], actor="test", db_path=db)
        verify_key(key, db_path=db, lastused_interval_seconds=86400)
        # Backdate last_used_at beyond the interval, then verify again.
        old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        conn = sqlite3.connect(str(db))
        conn.execute(
            "UPDATE api_keys SET last_used_at = ? WHERE key_id = ?",
            (old, record["key_id"]),
        )
        conn.commit()
        conn.close()
        verify_key(key, db_path=db, lastused_interval_seconds=86400)
        assert self._last_used(db, record["key_id"]) != old

    def test_last_used_zero_interval_updates_each_time(self, db: Path) -> None:
        key, record = create_key("alice@x", ["reader"], actor="test", db_path=db)
        verify_key(key, db_path=db, lastused_interval_seconds=0)
        first = self._last_used(db, record["key_id"])
        assert first is not None


# ---------------------------------------------------------------------------
# CLI rotate — nova server api-key rotate
# ---------------------------------------------------------------------------


class TestCliRotate:
    def test_rotate_cli_prints_successor_once_with_window(self, db: Path) -> None:
        from novafabric.cli.main import app

        _, record = create_key("alice@x", ["reader"], actor="test", db_path=db)
        result = runner.invoke(
            app,
            ["server", "api-key", "rotate", record["key_id"], "--db-path", str(db)],
        )
        assert result.exit_code == 0, result.output
        assert "shown once" in result.output.lower()
        assert "overlap" in result.output.lower()
        successor = result.output.strip().splitlines()[-1].strip()
        assert verify_key(successor, db_path=db) is not None

    def test_rotate_cli_unknown_key_fails(self, db: Path) -> None:
        from novafabric.cli.main import app

        result = runner.invoke(
            app, ["server", "api-key", "rotate", "nosuchkey", "--db-path", str(db)]
        )
        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# Auth integration: Bearer nvfk_... resolves via verify_key (RBAC end-to-end)
# ---------------------------------------------------------------------------


def _client(tmp_path: Path) -> TestClient:
    cfg = ServerConfig(db_path=str(tmp_path / "server.db"), local_token=TEST_TOKEN)
    return TestClient(create_app(cfg), raise_server_exceptions=False)


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class TestAuthIntegration:
    def test_reader_key_can_hit_reader_route(self, tmp_path: Path) -> None:
        client = _client(tmp_path)
        key, _ = create_key("alice@example.com", ["reader"], actor="test")
        resp = client.get("/v0/assets", headers=_bearer(key))
        assert resp.status_code == 200, resp.text

    def test_reader_key_cannot_hit_writer_route(self, tmp_path: Path) -> None:
        client = _client(tmp_path)
        key, _ = create_key("alice@example.com", ["reader"], actor="test")
        resp = client.post(
            "/v0/assets",
            json={"name": "m", "type": "model", "version": "1.0.0"},
            headers=_bearer(key),
        )
        assert resp.status_code == 403, resp.text

    def test_writer_key_can_hit_writer_route(self, tmp_path: Path) -> None:
        client = _client(tmp_path)
        key, _ = create_key("ci-bot", ["writer"], actor="test")
        resp = client.post(
            "/v0/assets",
            json={"name": "m", "type": "model", "version": "1.0.0"},
            headers=_bearer(key),
        )
        # Past authn (401) and RBAC (403) — the 400 is the route's own
        # body validation (spec_yaml required), which proves writer access.
        assert resp.status_code not in (401, 403), resp.text

    def test_revoked_key_gets_401_immediately(self, tmp_path: Path) -> None:
        client = _client(tmp_path)
        key, record = create_key("alice@example.com", ["reader"], actor="test")
        assert client.get("/v0/assets", headers=_bearer(key)).status_code == 200
        revoke_key(record["key_id"], actor="test")
        assert client.get("/v0/assets", headers=_bearer(key)).status_code == 401

    def test_invalid_nvfk_bearer_is_401_not_fallthrough(self, tmp_path: Path) -> None:
        client = _client(tmp_path)
        resp = client.get("/v0/assets", headers=_bearer("nvfk_deadbeef_" + "x" * 43))
        assert resp.status_code == 401

    def test_non_nvfk_tokens_follow_existing_path(self, tmp_path: Path) -> None:
        client = _client(tmp_path)
        # The ADR-0184 local token still authenticates unchanged.
        resp = client.get("/v0/assets", headers=_bearer(TEST_TOKEN))
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Secret-scanner rule (capture/secrets.py)
# ---------------------------------------------------------------------------


class TestScannerRule:
    def test_nvfk_rule_fires_on_seeded_key_in_capsule_text(
        self, tmp_path: Path, db: Path
    ) -> None:
        from novafabric.capture.secrets import SecretScannerV0

        key, _ = create_key("alice@example.com", ["reader"], actor="test", db_path=db)
        capsule_dir = tmp_path / "capsule"
        capsule_dir.mkdir()
        target = capsule_dir / "model-calls.jsonl"
        target.write_text(json.dumps({"messages": [f"my key is {key}"]}) + "\n")

        proof = SecretScannerV0(capsule_dir, "run-test").scan_and_redact()

        rule_ids = {f["rule_id"] for f in proof["findings"]}
        assert "novafabric-api-key" in rule_ids
        _, secret = parse_key(key)  # type: ignore[misc]
        redacted = target.read_text()
        assert secret not in redacted
        assert "[REDACTED:novafabric-api-key]" in redacted

    def test_redact_secrets_in_text_masks_nvfk(self, db: Path) -> None:
        from novafabric.capture.secrets import redact_secrets_in_text

        key, _ = create_key("alice@example.com", ["reader"], actor="test", db_path=db)
        out = redact_secrets_in_text(f"leak: {key}")
        assert key not in out
        assert "novafabric-api-key" in out


# ---------------------------------------------------------------------------
# CLI smoke — nova server api-key create|list|revoke
# ---------------------------------------------------------------------------

runner = CliRunner()


class TestCli:
    def _create(self, db: Path, *extra: str) -> tuple[str, str]:
        from novafabric.cli.main import app

        result = runner.invoke(
            app,
            [
                "server", "api-key", "create",
                "--owner", "alice@example.com",
                "--roles", "reader",
                "--db-path", str(db),
                *extra,
            ],
        )
        assert result.exit_code == 0, result.output
        key = result.output.strip().splitlines()[-1].strip()
        key_id, _ = parse_key(key)  # type: ignore[misc]
        return key, key_id

    def test_create_prints_key_once_with_warning(self, db: Path) -> None:
        from novafabric.cli.main import app

        result = runner.invoke(
            app,
            [
                "server", "api-key", "create",
                "--owner", "alice@example.com",
                "--roles", "reader,auditor",
                "--db-path", str(db),
            ],
        )
        assert result.exit_code == 0, result.output
        assert "shown once" in result.output.lower()
        key = result.output.strip().splitlines()[-1].strip()
        assert verify_key(key, db_path=db) is not None

    def test_create_rejects_bad_role(self, db: Path) -> None:
        from novafabric.cli.main import app

        result = runner.invoke(
            app,
            [
                "server", "api-key", "create",
                "--owner", "alice@example.com",
                "--roles", "superuser",
                "--db-path", str(db),
            ],
        )
        assert result.exit_code != 0

    def test_list_shows_key_id_but_no_secret(self, db: Path) -> None:
        from novafabric.cli.main import app

        key, key_id = self._create(db)
        _, secret = parse_key(key)  # type: ignore[misc]
        result = runner.invoke(app, ["server", "api-key", "list", "--db-path", str(db)])
        assert result.exit_code == 0, result.output
        assert key_id in result.output
        assert secret not in result.output

    def test_revoke_by_key_id(self, db: Path) -> None:
        from novafabric.cli.main import app

        key, key_id = self._create(db)
        result = runner.invoke(
            app, ["server", "api-key", "revoke", key_id, "--db-path", str(db)]
        )
        assert result.exit_code == 0, result.output
        assert verify_key(key, db_path=db) is None

    def test_revoke_unknown_key_id_fails(self, db: Path) -> None:
        from novafabric.cli.main import app

        result = runner.invoke(
            app, ["server", "api-key", "revoke", "nosuchkey", "--db-path", str(db)]
        )
        assert result.exit_code == 1

    def test_list_empty_store(self, db: Path) -> None:
        from novafabric.cli.main import app

        result = runner.invoke(app, ["server", "api-key", "list", "--db-path", str(db)])
        assert result.exit_code == 0, result.output
        assert "no api keys" in result.output.lower()
