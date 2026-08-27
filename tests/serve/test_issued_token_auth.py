"""ADR-0252 — a token `nova serve` issues must actually authenticate.

Measured before the fix, on a live app:

    POST /api/admin/tokens              -> 200 {"token": ..., "warning":
                                          "Save this token — it will not be
                                           shown again."}
    GET  /api/runs  with that token     -> 401 missing or invalid token
    ~/.novafabric/tokens.jsonl          -> mode 0664, secret stored verbatim

`verify_token` compared only against the single server token, so the endpoint
minted a credential that worked nowhere and then wrote it, in cleartext and
world-readable, next to a `.serve-token` that `auth.py` deliberately creates at
0600.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from novafabric.serve import token_store  # noqa: E402
from novafabric.serve.app import create_app  # noqa: E402

SERVER_TOKEN = "server-token-0123456789abcdef"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """A serve app whose token store lives under tmp_path."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("NOVAFABRIC_HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    app = create_app(token=SERVER_TOKEN, capsule_dir=tmp_path)
    return TestClient(app, base_url="http://127.0.0.1")


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _issue(client, label="ci-bot") -> tuple[str, str]:
    response = client.post(
        "/api/admin/tokens",
        json={"label": label, "confirmed": True},
        headers=_auth(SERVER_TOKEN),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    return body["token"], body["fingerprint"]


class TestAnIssuedTokenWorks:
    """The regression guard: every one of these returned 401 before."""

    def test_bearer_header(self, client):
        issued, _ = _issue(client)
        assert client.get("/api/runs", headers=_auth(issued)).status_code == 200

    def test_query_parameter_form(self, client):
        issued, _ = _issue(client)
        assert client.get(f"/api/runs?token={issued}").status_code == 200

    def test_it_can_reach_a_mutating_endpoint_too(self, client):
        """Authentication is all-or-nothing here; an issued token is not lesser."""
        issued, _ = _issue(client)
        response = client.post(
            "/api/admin/tokens",
            json={"label": "second", "confirmed": True},
            headers=_auth(issued),
        )
        assert response.status_code == 200, response.text

    def test_the_response_says_so_rather_than_implying_a_scope(self, client):
        response = client.post(
            "/api/admin/tokens",
            json={"label": "ci-bot", "confirmed": True},
            headers=_auth(SERVER_TOKEN),
        )
        warning = response.json()["warning"]
        assert "same full access" in warning
        assert "does not authorize" in warning


class TestOnlyRealTokensWork:
    def test_a_bogus_token_is_rejected(self, client):
        _issue(client)
        assert client.get("/api/runs", headers=_auth("nope")).status_code == 401

    def test_no_token_is_rejected(self, client):
        assert client.get("/api/runs").status_code == 401

    def test_the_server_token_still_works(self, client):
        assert client.get("/api/runs", headers=_auth(SERVER_TOKEN)).status_code == 200


class TestRevocationTakesEffect:
    def test_a_revoked_token_stops_working(self, client):
        issued, fingerprint = _issue(client)
        assert client.get("/api/runs", headers=_auth(issued)).status_code == 200
        assert (
            client.delete(
                f"/api/admin/tokens/{fingerprint}", headers=_auth(SERVER_TOKEN)
            ).status_code
            == 200
        )
        assert client.get("/api/runs", headers=_auth(issued)).status_code == 401

    def test_revoking_one_does_not_affect_another(self, client):
        first, first_fp = _issue(client, "first")
        second, _ = _issue(client, "second")
        client.delete(f"/api/admin/tokens/{first_fp}", headers=_auth(SERVER_TOKEN))
        assert client.get("/api/runs", headers=_auth(first)).status_code == 401
        assert client.get("/api/runs", headers=_auth(second)).status_code == 200

    def test_revoking_an_unknown_fingerprint_is_404(self, client):
        _issue(client)
        response = client.delete(
            "/api/admin/tokens/deadbeefdeadbeef", headers=_auth(SERVER_TOKEN)
        )
        assert response.status_code == 404


class TestTheSecretIsNotOnDisk:
    def test_the_record_stores_a_digest_not_the_token(self, client, tmp_path):
        issued, _ = _issue(client)
        record = json.loads(
            (tmp_path / ".novafabric" / "tokens.jsonl").read_text().splitlines()[0]
        )
        assert "token" not in record
        assert record["token_digest"] == token_store.digest(issued)
        assert issued not in json.dumps(record)

    def test_the_file_is_not_world_readable(self, client, tmp_path):
        _issue(client)
        path = tmp_path / ".novafabric" / "tokens.jsonl"
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_a_revoke_does_not_widen_the_mode(self, client, tmp_path):
        """The old rewrite created its temp file at the umask, undoing 0600."""
        _, fingerprint = _issue(client)
        client.delete(f"/api/admin/tokens/{fingerprint}", headers=_auth(SERVER_TOKEN))
        path = tmp_path / ".novafabric" / "tokens.jsonl"
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_the_list_endpoint_still_hides_everything_secret(self, client):
        issued, _ = _issue(client)
        body = client.get("/api/admin/tokens", headers=_auth(SERVER_TOKEN)).json()
        assert issued not in json.dumps(body)
        assert all("token_digest" not in row for row in body["tokens"])


class TestLegacyRecordsWrittenBeforeThisAdr:
    """Locking an operator out to punish them for an old file is the wrong trade."""

    def test_a_plaintext_record_still_authenticates(self, client, tmp_path):
        legacy = "legacy-plaintext-token-value"
        path = tmp_path / ".novafabric" / "tokens.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "label": "old",
                    "token": legacy,
                    "fingerprint": token_store.fingerprint(legacy),
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "revoked": False,
                }
            )
            + "\n"
        )
        assert client.get("/api/runs", headers=_auth(legacy)).status_code == 200

    def test_reading_one_hardens_the_file(self, client, tmp_path):
        legacy = "legacy-plaintext-token-value"
        path = tmp_path / ".novafabric" / "tokens.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {"label": "old", "token": legacy, "fingerprint": "x", "revoked": False}
            )
            + "\n"
        )
        path.chmod(0o664)
        client.get("/api/runs", headers=_auth(legacy))
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_they_are_countable_so_an_operator_can_be_told(self, client, tmp_path):
        path = tmp_path / ".novafabric" / "tokens.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"label": "old", "token": "a", "fingerprint": "x"}) + "\n"
        )
        assert token_store.legacy_plaintext_count() == 1
        _issue(client)
        assert token_store.legacy_plaintext_count() == 1


class TestAuditTrail:
    def test_it_no_longer_cites_a_command_that_does_not_exist(self, client):
        """`nova server issue-token` mints an unrelated JWT and has no --label."""
        _issue(client)
        body = client.get("/api/audit", headers=_auth(SERVER_TOKEN)).json()
        entries = body.get("entries", body.get("events", []))
        issued = [e for e in entries if e.get("action") == "issue_token"]
        assert issued, entries
        assert all("--label" not in (e.get("cli_equivalent") or "") for e in issued)
