"""Tests for the /v0/seal/* REST routes (NovaSeal maker-checker, ADR-0059)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from novafabric.server.app import create_app  # noqa: E402
from novafabric.server.config import ServerConfig  # noqa: E402

FIXTURE_KEYS = Path(__file__).parent / "fixtures" / "promote" / "keys"


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    cfg = ServerConfig(db_path=str(tmp_path / "test.db"))
    app = create_app(cfg)
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# GET /v0/seal/policy
# ---------------------------------------------------------------------------


class TestSealPolicy:
    def test_no_policy_returns_404(self, client: TestClient) -> None:
        from novafabric.promote.exceptions import PolicyNotFoundError

        with patch("novafabric.server.routes.seal.PolicyStore") as MockPS:
            inst = MockPS.return_value
            inst.get_latest_version.side_effect = PolicyNotFoundError("none")
            inst.get_latest.side_effect = PolicyNotFoundError("none")
            r = client.get("/v0/seal/policy")
        assert r.status_code == 404

    def test_policy_returned_as_raw_predicate(self, client: TestClient, tmp_path: Path) -> None:
        from novafabric.promote.policy_store import PolicyStore

        predicate = {
            "proposer_key_ids": ["alice"],
            "approver_key_ids": ["bob"],
            "bypass_valid_duration_hours": 24,
        }
        db_path = tmp_path / "policy.db"
        ps = PolicyStore(db_path)
        ps.put(json.dumps(predicate))
        ps.close()

        with patch("novafabric.server.routes.seal._merkle_db_path", return_value=db_path):
            r = client.get("/v0/seal/policy")

        assert r.status_code == 200
        body = r.json()
        assert body["version"] == 1
        assert body["predicate"]["proposer_key_ids"] == ["alice"]
        assert body["created_at"]  # non-empty timestamp

    def test_policy_returned_from_dsse_envelope(self, client: TestClient, tmp_path: Path) -> None:
        import base64

        from novafabric.promote.policy_store import PolicyStore

        predicate = {"proposer_key_ids": ["alice"], "approver_key_ids": ["bob"]}
        payload_b64 = (
            base64.urlsafe_b64encode(json.dumps(predicate).encode()).rstrip(b"=").decode()
        )
        envelope = {
            "payloadType": "application/vnd.novafabric.promote.policy+json",
            "payload": payload_b64,
        }
        db_path = tmp_path / "policy2.db"
        ps = PolicyStore(db_path)
        ps.put(json.dumps(envelope))
        ps.close()

        with patch("novafabric.server.routes.seal._merkle_db_path", return_value=db_path):
            r = client.get("/v0/seal/policy")

        assert r.status_code == 200
        body = r.json()
        assert body["version"] == 1
        assert body["predicate"]["approver_key_ids"] == ["bob"]


# ---------------------------------------------------------------------------
# GET /v0/seal/{capsule_id}/proposals
# ---------------------------------------------------------------------------


class TestSealProposals:
    def test_empty_capsule_returns_empty_list(self, client: TestClient) -> None:
        with patch("novafabric.server.routes.seal.PromoteBundleStore") as MockBS:
            inst = MockBS.return_value
            inst.list_proposals.return_value = []
            r = client.get("/v0/seal/test-cap-001/proposals")
        assert r.status_code == 200
        assert r.json() == []

    def test_proposals_without_approval(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        from novafabric.promote.bundle_store import PromoteBundleStore
        from novafabric.promote.policy_store import PolicyStore
        from novafabric.promote.predicates import build_proposal_predicate, sign_promote_envelope

        # Use real fixtures to create a valid proposal bundle
        policy_store = PolicyStore(tmp_path / "m.db")
        policy_store.put(
            json.dumps({"proposer_key_ids": ["proposer"], "approver_key_ids": ["approver"]})
        )
        bundle_store = PromoteBundleStore(tmp_path)

        predicate = build_proposal_predicate(
            capsule_id="cap-abc",
            capsule_digest_hex="deadbeef",
            target_env="staging",
            justification="This is a test justification that is long enough.",
            proposer_subject="proposer",
            policy_version=1,
        )
        envelope_bytes = sign_promote_envelope(
            json.dumps(predicate).encode(),
            "application/vnd.novafabric.promote.proposal+json",
            FIXTURE_KEYS / "proposer.pem",
            FIXTURE_KEYS / "proposer_cert.pem",
        )
        bundle_store.put_proposal("cap-abc", envelope_bytes)

        with (
            patch("novafabric.server.routes.seal.PromoteBundleStore", return_value=bundle_store),
            patch("novafabric.server.routes.seal._data_dir", return_value=tmp_path),
        ):
            r = client.get("/v0/seal/cap-abc/proposals")

        assert r.status_code == 200
        items = r.json()
        assert len(items) == 1
        assert items[0]["proposer_subject"] == "proposer"
        assert items[0]["has_approval"] is False
        assert items[0]["justification"] == "This is a test justification that is long enough."

    def test_corrupt_proposal_envelope_included_with_defaults(self, client: TestClient) -> None:
        from novafabric.promote.bundle_store import PromoteBundleStore
        from novafabric.promote.exceptions import BundleNotFoundError

        mock_bs = MagicMock(spec=PromoteBundleStore)
        mock_bs.list_proposals.return_value = ["uuid-x"]
        mock_bs.get_proposal.return_value = b"not-a-dsse-envelope"
        mock_bs.get_approval.side_effect = BundleNotFoundError("no approval")

        with patch("novafabric.server.routes.seal.PromoteBundleStore", return_value=mock_bs):
            r = client.get("/v0/seal/cap-xyz/proposals")

        assert r.status_code == 200
        items = r.json()
        assert len(items) == 1
        assert items[0]["uuid"] == "uuid-x"
        assert items[0]["has_approval"] is False
        assert items[0]["proposer_subject"] == ""

    def test_proposals_with_approval(self, client: TestClient, tmp_path: Path) -> None:
        from novafabric.promote.bundle_store import PromoteBundleStore
        from novafabric.promote.predicates import (
            build_approval_predicate,
            build_proposal_predicate,
            sign_promote_envelope,
        )

        bundle_store = PromoteBundleStore(tmp_path)
        predicate = build_proposal_predicate(
            capsule_id="cap-with-approval",
            capsule_digest_hex="cafebabe",
            target_env="prod",
            justification="Approved proposal test justification meets minimum.",
            proposer_subject="proposer",
            policy_version=1,
        )
        proposal_bytes = sign_promote_envelope(
            json.dumps(predicate).encode(),
            "application/vnd.novafabric.promote.proposal+json",
            FIXTURE_KEYS / "proposer.pem",
            FIXTURE_KEYS / "proposer_cert.pem",
        )
        bundle_store.put_proposal("cap-with-approval", proposal_bytes)

        approval_predicate = build_approval_predicate(
            proposal_envelope_bytes=proposal_bytes,
            decision="approved",
            approver_subject="approver",
        )
        approval_bytes = sign_promote_envelope(
            json.dumps(approval_predicate).encode(),
            "application/vnd.novafabric.promote.approval+json",
            FIXTURE_KEYS / "approver.pem",
            FIXTURE_KEYS / "approver_cert.pem",
        )
        proposal_uuid = bundle_store.list_proposals("cap-with-approval")[0]
        bundle_store.put_approval("cap-with-approval", approval_bytes, proposal_uuid)

        with patch("novafabric.server.routes.seal.PromoteBundleStore", return_value=bundle_store):
            r = client.get("/v0/seal/cap-with-approval/proposals")

        assert r.status_code == 200
        items = r.json()
        assert len(items) == 1
        assert items[0]["has_approval"] is True
        assert items[0]["approver_subject"] == "approver"


# ---------------------------------------------------------------------------
# POST /v0/seal/{capsule_id}/verify
# ---------------------------------------------------------------------------


class TestSealVerify:
    def test_verify_pass(self, client: TestClient) -> None:
        from novafabric.promote.verifier import VerifyResult

        mock_result = VerifyResult(passed=True, exit_code=0, message="SoD verification passed")

        with (
            patch("novafabric.server.routes.seal.verify_sod", return_value=mock_result),
            patch("novafabric.server.routes.seal.PolicyStore"),
            patch("novafabric.server.routes.seal.PromoteBundleStore"),
        ):
            r = client.post("/v0/seal/cap-pass/verify")

        assert r.status_code == 200
        body = r.json()
        assert body["passed"] is True
        assert body["exit_code"] == 0
        assert len(body["check_results"]) == 5
        assert all(c["passed"] for c in body["check_results"])

    def test_verify_fail_check_3_self_approval(self, client: TestClient) -> None:
        from novafabric.promote.verifier import VerifyResult

        mock_result = VerifyResult(
            passed=False, exit_code=6, message="proposer and approver are the same identity"
        )

        with (
            patch("novafabric.server.routes.seal.verify_sod", return_value=mock_result),
            patch("novafabric.server.routes.seal.PolicyStore"),
            patch("novafabric.server.routes.seal.PromoteBundleStore"),
        ):
            r = client.post("/v0/seal/cap-fail/verify")

        assert r.status_code == 200
        body = r.json()
        assert body["passed"] is False
        assert body["exit_code"] == 6
        # Checks 1-3 present; check 4 (index 3 = exit_code 6) is the failed one
        checks = body["check_results"]
        assert len(checks) == 4
        assert checks[-1]["passed"] is False
        assert "same" in checks[-1]["message"]

    def test_verify_missing_proposal_returns_no_checks(self, client: TestClient) -> None:
        from novafabric.promote.verifier import VerifyResult

        mock_result = VerifyResult(passed=False, exit_code=9, message="No proposal found")

        with (
            patch("novafabric.server.routes.seal.verify_sod", return_value=mock_result),
            patch("novafabric.server.routes.seal.PolicyStore"),
            patch("novafabric.server.routes.seal.PromoteBundleStore"),
        ):
            r = client.post("/v0/seal/cap-empty/verify")

        assert r.status_code == 200
        body = r.json()
        assert body["passed"] is False
        assert body["exit_code"] == 9
        assert body["check_results"] == []
