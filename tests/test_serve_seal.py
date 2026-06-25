"""Tests for the /api/seal/* endpoints in nova serve --experimental (serve/app.py)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from novafabric.serve.app import create_app  # noqa: E402

_TOKEN = "test-serve-seal-token"
FIXTURE_KEYS = Path(__file__).parent / "fixtures" / "promote" / "keys"
# serve/app.py host guard requires localhost; TestClient sends "testserver" by default
_LOCALHOST = {"host": "localhost"}


@pytest.fixture()
def serve_client(tmp_path: Path) -> TestClient:
    capsule_dir = tmp_path / "runs"
    capsule_dir.mkdir()
    app = create_app(
        token=_TOKEN,
        capsule_dir=capsule_dir,
        db_path=tmp_path / "registry.db",
    )
    return TestClient(app, raise_server_exceptions=False)


def _url(path: str, token: str = _TOKEN) -> str:
    return f"{path}?token={token}"


# ---------------------------------------------------------------------------
# GET /api/seal/policy
# ---------------------------------------------------------------------------


class TestServeGetPolicy:
    def test_unauthenticated_returns_401(self, serve_client: TestClient) -> None:
        r = serve_client.get("/api/seal/policy", headers=_LOCALHOST)
        assert r.status_code == 401

    def test_non_localhost_host_returns_403(self, serve_client: TestClient) -> None:
        r = serve_client.get(_url("/api/seal/policy"), headers={"host": "evil.example.com"})
        assert r.status_code == 403

    def test_no_policy_returns_200_unconfigured(
        self, serve_client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # No signed policy → 200 with configured=false (not 404), so the
        # read-only dashboard renders a clean empty state without a console error.
        db = tmp_path / "empty.db"
        db.touch()
        monkeypatch.setenv("NOVAFABRIC_SEAL_DB_PATH", str(db))
        r = serve_client.get(_url("/api/seal/policy"), headers=_LOCALHOST)
        assert r.status_code == 200
        body = r.json()
        assert body["configured"] is False
        assert body["predicate"] is None

    def test_policy_returned(
        self, serve_client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from novafabric.promote.policy_store import PolicyStore

        predicate = {
            "proposer_key_ids": ["alice"],
            "approver_key_ids": ["bob"],
            "bypass_valid_duration_hours": 24,
        }
        db = tmp_path / "policy.db"
        ps = PolicyStore(db)
        ps.put(json.dumps(predicate))
        ps.close()

        monkeypatch.setenv("NOVAFABRIC_SEAL_DB_PATH", str(db))
        r = serve_client.get(_url("/api/seal/policy"), headers=_LOCALHOST)

        assert r.status_code == 200
        body = r.json()
        assert body["version"] == 1
        assert body["predicate"]["proposer_key_ids"] == ["alice"]
        assert body["created_at"]

    def test_policy_from_dsse_envelope(
        self, serve_client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import base64

        from novafabric.promote.policy_store import PolicyStore

        predicate = {"proposer_key_ids": ["carol"], "approver_key_ids": ["dave"]}
        payload_b64 = (
            base64.urlsafe_b64encode(json.dumps(predicate).encode()).rstrip(b"=").decode()
        )
        envelope = {
            "payloadType": "application/vnd.novafabric.promote.policy+json",
            "payload": payload_b64,
        }
        db = tmp_path / "policy_dsse.db"
        ps = PolicyStore(db)
        ps.put(json.dumps(envelope))
        ps.close()

        monkeypatch.setenv("NOVAFABRIC_SEAL_DB_PATH", str(db))
        r = serve_client.get(_url("/api/seal/policy"), headers=_LOCALHOST)

        assert r.status_code == 200
        body = r.json()
        assert body["predicate"]["approver_key_ids"] == ["dave"]


# ---------------------------------------------------------------------------
# GET /api/seal/{capsule_id}/proposals
# ---------------------------------------------------------------------------


class TestServeListProposals:
    def test_empty_returns_empty_list(
        self, serve_client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NOVAFABRIC_HOME", str(tmp_path))
        r = serve_client.get(_url("/api/seal/cap-001/proposals"), headers=_LOCALHOST)
        assert r.status_code == 200
        assert r.json() == []

    def test_proposals_returned(
        self, serve_client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from novafabric.promote.bundle_store import PromoteBundleStore
        from novafabric.promote.predicates import build_proposal_predicate, sign_promote_envelope

        monkeypatch.setenv("NOVAFABRIC_HOME", str(tmp_path))

        bs = PromoteBundleStore(tmp_path)
        pred = build_proposal_predicate(
            capsule_id="cap-001",
            capsule_digest_hex="deadbeef",
            target_env="staging",
            justification="Serve-mode proposal test — justification meets minimum length.",
            proposer_subject="proposer",
            policy_version=1,
        )
        env_bytes = sign_promote_envelope(
            json.dumps(pred).encode(),
            "application/vnd.novafabric.promote.proposal+json",
            FIXTURE_KEYS / "proposer.pem",
            FIXTURE_KEYS / "proposer_cert.pem",
        )
        bs.put_proposal("cap-001", env_bytes)

        r = serve_client.get(_url("/api/seal/cap-001/proposals"), headers=_LOCALHOST)
        assert r.status_code == 200
        items = r.json()
        assert len(items) == 1
        assert items[0]["proposer_subject"] == "proposer"
        assert items[0]["has_approval"] is False

    def test_proposals_with_approval(
        self, serve_client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from novafabric.promote.bundle_store import PromoteBundleStore
        from novafabric.promote.predicates import (
            build_approval_predicate,
            build_proposal_predicate,
            sign_promote_envelope,
        )

        monkeypatch.setenv("NOVAFABRIC_HOME", str(tmp_path))

        bs = PromoteBundleStore(tmp_path)
        pred = build_proposal_predicate(
            capsule_id="cap-approve",
            capsule_digest_hex="feedface",
            target_env="prod",
            justification="Serve approval test — proposal-plus-approval happy path.",
            proposer_subject="proposer",
            policy_version=1,
        )
        proposal_bytes = sign_promote_envelope(
            json.dumps(pred).encode(),
            "application/vnd.novafabric.promote.proposal+json",
            FIXTURE_KEYS / "proposer.pem",
            FIXTURE_KEYS / "proposer_cert.pem",
        )
        proposal_uuid = bs.put_proposal("cap-approve", proposal_bytes)

        approval_pred = build_approval_predicate(
            proposal_envelope_bytes=proposal_bytes,
            decision="approved",
            approver_subject="approver",
        )
        approval_bytes = sign_promote_envelope(
            json.dumps(approval_pred).encode(),
            "application/vnd.novafabric.promote.approval+json",
            FIXTURE_KEYS / "approver.pem",
            FIXTURE_KEYS / "approver_cert.pem",
        )
        bs.put_approval("cap-approve", approval_bytes, proposal_uuid)

        r = serve_client.get(_url("/api/seal/cap-approve/proposals"), headers=_LOCALHOST)
        assert r.status_code == 200
        items = r.json()
        assert len(items) == 1
        assert items[0]["has_approval"] is True
        assert items[0]["approver_subject"] == "approver"


# ---------------------------------------------------------------------------
# POST /api/seal/{capsule_id}/verify
# ---------------------------------------------------------------------------


class TestServeVerifySeal:
    def test_verify_pass(
        self, serve_client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from novafabric.promote.verifier import VerifyResult

        db = tmp_path / "m.db"
        monkeypatch.setenv("NOVAFABRIC_SEAL_DB_PATH", str(db))
        monkeypatch.setenv("NOVAFABRIC_HOME", str(tmp_path))

        mock_result = VerifyResult(passed=True, exit_code=0, message="SoD verification passed")
        with patch("novafabric.promote.verifier.verify_sod", return_value=mock_result):
            r = serve_client.post(_url("/api/seal/cap-pass/verify"), headers=_LOCALHOST)

        assert r.status_code == 200
        body = r.json()
        assert body["passed"] is True
        assert len(body["check_results"]) == 5

    def test_verify_fail_self_approval(
        self, serve_client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from novafabric.promote.verifier import VerifyResult

        db = tmp_path / "m.db"
        monkeypatch.setenv("NOVAFABRIC_SEAL_DB_PATH", str(db))
        monkeypatch.setenv("NOVAFABRIC_HOME", str(tmp_path))

        mock_result = VerifyResult(
            passed=False,
            exit_code=6,
            message="proposer and approver are the same identity",
        )
        with patch("novafabric.promote.verifier.verify_sod", return_value=mock_result):
            r = serve_client.post(_url("/api/seal/cap-fail/verify"), headers=_LOCALHOST)

        assert r.status_code == 200
        body = r.json()
        assert body["passed"] is False
        assert body["exit_code"] == 6
        checks = body["check_results"]
        assert checks[-1]["passed"] is False

    def test_verify_missing_proposal(
        self, serve_client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from novafabric.promote.verifier import VerifyResult

        db = tmp_path / "m.db"
        monkeypatch.setenv("NOVAFABRIC_SEAL_DB_PATH", str(db))
        monkeypatch.setenv("NOVAFABRIC_HOME", str(tmp_path))

        mock_result = VerifyResult(passed=False, exit_code=9, message="No proposal found")
        with patch("novafabric.promote.verifier.verify_sod", return_value=mock_result):
            r = serve_client.post(_url("/api/seal/cap-empty/verify"), headers=_LOCALHOST)

        assert r.status_code == 200
        body = r.json()
        assert body["passed"] is False
        assert body["check_results"] == []
