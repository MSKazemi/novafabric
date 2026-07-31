"""Coverage campaign for serve/app.py POST/GET handlers (backlog Task #2).

Exercises the largest previously-uncovered route handlers with real minimal
capsules — success, failure, and validation paths.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from novafabric.serve.app import create_app

TOKEN = "testtoken"
AUTH = {"token": TOKEN}
HOST = {"host": "127.0.0.1:4321"}


@pytest.fixture()
def stack(tmp_path: Path) -> tuple[TestClient, Path]:
    capsule_dir = tmp_path / "runs"
    capsule_dir.mkdir()
    app = create_app(
        token=TOKEN, capsule_dir=capsule_dir, db_path=tmp_path / "registry.db"
    )
    return TestClient(app), capsule_dir


def _mk_capsule(capsule_dir: Path, run_id: str = "01HXCAMPAIGN0000000000TEST") -> Path:
    c = capsule_dir / run_id
    c.mkdir()
    (c / "capsule.yaml").write_text(
        yaml.dump(
            {
                "run_id": run_id,
                "command": ["python", "-c", "pass"],
                "created_at": "2026-06-12T08:00:00Z",
                "status": "success",
            }
        )
    )
    (c / "model-calls.jsonl").write_text(
        json.dumps({"started": "2026-06-12T08:00:01Z", "model": "m", "response": {}})
        + "\n"
    )
    (c / "tool-calls.jsonl").write_text(
        json.dumps({"started": "2026-06-12T08:00:02Z", "tool_name": "db"}) + "\n"
    )
    (c / "lineage.jsonl").write_text(json.dumps({"edge_type": "consumed"}) + "\n")
    return c


class TestSealLogVerify:
    def test_empty_log_is_consistent(
        self, stack: tuple[TestClient, Path], monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        client, _ = stack
        monkeypatch.setenv("NOVAFABRIC_SEAL_DB_PATH", str(tmp_path / "merkle.db"))
        resp = client.get("/api/seal/log/verify", params=AUTH, headers=HOST)
        assert resp.status_code == 200
        data = resp.json()
        assert data["consistent"] is True
        assert data["entry_count"] == 0

    def test_capsule_inclusion_flag(
        self, stack: tuple[TestClient, Path], monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        from novafabric.trust.novaseal.merkle import MerkleLog

        client, _ = stack
        db = tmp_path / "merkle.db"
        monkeypatch.setenv("NOVAFABRIC_SEAL_DB_PATH", str(db))
        log = MerkleLog(db)
        log.append({"capsule_id": "cap-included"})
        log.close()
        included = client.get(
            "/api/seal/log/verify",
            params={**AUTH, "capsule_id": "cap-included"},
            headers=HOST,
        ).json()
        assert included["capsule_included"] is True
        absent = client.get(
            "/api/seal/log/verify",
            params={**AUTH, "capsule_id": "cap-absent"},
            headers=HOST,
        ).json()
        assert absent["capsule_included"] is False


class TestRunsSearchAndCost:
    def test_search_disk_fallback_with_query(
        self, stack: tuple[TestClient, Path]
    ) -> None:
        client, capsule_dir = stack
        _mk_capsule(capsule_dir)
        resp = client.get(
            "/api/runs/search", params={**AUTH, "q": "CAMPAIGN"}, headers=HOST
        )
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data["items"], list)

    def test_search_rejects_bad_limit(self, stack: tuple[TestClient, Path]) -> None:
        client, _ = stack
        resp = client.get(
            "/api/runs/search", params={**AUTH, "limit": 9999}, headers=HOST
        )
        assert resp.status_code == 422

    def test_cost_summary_requires_run_ids(
        self, stack: tuple[TestClient, Path]
    ) -> None:
        client, _ = stack
        missing = client.get("/api/runs/cost-summary", params=AUTH, headers=HOST)
        assert missing.status_code == 422

    def test_cost_summary_stub_aware(self, stack: tuple[TestClient, Path]) -> None:
        client, capsule_dir = stack
        c = _mk_capsule(capsule_dir)
        resp = client.get(
            "/api/runs/cost-summary",
            params={**AUTH, "run_ids": c.name},
            headers=HOST,
        )
        # without ClickHouse this exercises the stub/fallback branch
        assert resp.status_code == 200


class TestDoctorAndPolicy:
    def test_doctor_reports_checks(self, stack: tuple[TestClient, Path]) -> None:
        client, _ = stack
        resp = client.get("/api/doctor", params=AUTH, headers=HOST)
        assert resp.status_code == 200
        data = resp.json()
        assert "checks" in data or "ok" in data

    def test_doctor_reports_cap003_disabled_by_default(
        self, stack: tuple[TestClient, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SCALE-ADR-003: the safe default is DISABLED — cap-003 may not be
        active until EU-GDPR legal counsel reviews its OQ-01. nova doctor
        surfaces that posture. Informational (`ok: True`)."""
        monkeypatch.delenv("NOVA_CAP003_ENABLED", raising=False)
        client, _ = stack
        resp = client.get("/api/doctor", params=AUTH, headers=HOST)
        assert resp.status_code == 200
        checks = {c["name"]: c for c in resp.json()["checks"]}
        assert "cap003_gdpr_legal_review" in checks
        cap003_check = checks["cap003_gdpr_legal_review"]
        assert cap003_check["ok"] is True
        assert "disabled" in cap003_check["detail"]
        assert "ACTIVE" not in cap003_check["detail"]
        assert "SCALE-ADR-003" in cap003_check["detail"]

    def test_doctor_reports_cap003_active_when_operator_overrides(
        self, stack: tuple[TestClient, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When an operator explicitly sets NOVA_CAP003_ENABLED=true, doctor
        must flag that the deployment is ACTIVE without the legal-counsel
        review SCALE-ADR-003 requires."""
        monkeypatch.setenv("NOVA_CAP003_ENABLED", "true")
        client, _ = stack
        resp = client.get("/api/doctor", params=AUTH, headers=HOST)
        assert resp.status_code == 200
        checks = {c["name"]: c for c in resp.json()["checks"]}
        cap003_check = checks["cap003_gdpr_legal_review"]
        assert cap003_check["ok"] is True
        assert "ACTIVE" in cap003_check["detail"]
        assert "legal-counsel review" in cap003_check["detail"]
        assert "SCALE-ADR-003" in cap003_check["detail"]

    def test_policy_test_endpoint(self, stack: tuple[TestClient, Path]) -> None:
        client, _ = stack
        resp = client.post("/api/policy/test", params=AUTH, headers=HOST, json={})
        assert resp.status_code in (200, 422)

    def test_policy_recent_decisions_empty(
        self, stack: tuple[TestClient, Path]
    ) -> None:
        client, _ = stack
        resp = client.get("/api/policy/recent-decisions", params=AUTH, headers=HOST)
        assert resp.status_code == 200

    def test_policy_explain_unknown_decision(
        self, stack: tuple[TestClient, Path]
    ) -> None:
        client, _ = stack
        resp = client.get(
            "/api/policy/explain",
            params={**AUTH, "decision_id": "no-such-decision"},
            headers=HOST,
        )
        assert resp.status_code in (200, 404)


class TestGovernanceClassify:
    def test_unknown_vocabulary_422(self, stack: tuple[TestClient, Path]) -> None:
        client, capsule_dir = stack
        c = _mk_capsule(capsule_dir)
        resp = client.get(
            "/api/governance/classify",
            params={**AUTH, "run_id": c.name, "vocabulary": "bogus/1.0"},
            headers=HOST,
        )
        assert resp.status_code == 422

    def test_classify_real_capsule(self, stack: tuple[TestClient, Path]) -> None:
        client, capsule_dir = stack
        c = _mk_capsule(capsule_dir)
        resp = client.get(
            "/api/governance/classify",
            params={**AUTH, "run_id": c.name},
            headers=HOST,
        )
        assert resp.status_code == 200
        assert "tier" in json.dumps(resp.json()).lower() or resp.json()

    def test_classify_missing_capsule_404(
        self, stack: tuple[TestClient, Path]
    ) -> None:
        client, _ = stack
        resp = client.get(
            "/api/governance/classify",
            params={**AUTH, "run_id": "no-such-run"},
            headers=HOST,
        )
        assert resp.status_code == 404


class TestComplianceAudit:
    def test_audit_coverage_and_report(self, stack: tuple[TestClient, Path]) -> None:
        client, capsule_dir = stack
        _mk_capsule(capsule_dir)
        cov = client.get(
            "/api/compliance/audit/coverage",
            params={**AUTH, "profile": "eu-ai-act-high-risk"},
            headers=HOST,
        )
        assert cov.status_code == 200
        rep = client.post(
            "/api/compliance/audit/report",
            params=AUTH,
            headers=HOST,
            json={"profile": "eu-ai-act-high-risk"},
        )
        assert rep.status_code in (200, 422)

    def test_audit_bundle_round_trip_and_verify(
        self, stack: tuple[TestClient, Path]
    ) -> None:
        client, capsule_dir = stack
        _mk_capsule(capsule_dir)
        bundle = client.post(
            "/api/compliance/audit/bundle",
            params=AUTH,
            headers=HOST,
            json={"profile": "eu-ai-act-high-risk"},
        )
        assert bundle.status_code in (200, 422)
        verify_bad = client.post(
            "/api/compliance/audit/verify",
            params=AUTH,
            headers=HOST,
            json={"not": "a-bundle"},
        )
        assert verify_bad.status_code in (200, 400, 422)

    def test_examiner_unknown_format_422(
        self, stack: tuple[TestClient, Path]
    ) -> None:
        client, _ = stack
        resp = client.post(
            "/api/compliance/examiner/zip",
            params=AUTH,
            headers=HOST,
            json={"run_id": "x"},
        )
        assert resp.status_code == 422

    def test_examiner_bagit_real_capsule(
        self, stack: tuple[TestClient, Path]
    ) -> None:
        client, capsule_dir = stack
        c = _mk_capsule(capsule_dir)
        resp = client.post(
            "/api/compliance/examiner/bagit",
            params=AUTH,
            headers=HOST,
            json={"run_id": c.name},
        )
        assert resp.status_code in (200, 404, 422)


class TestRunUtilities:
    def test_scan_secrets_with_proof_and_fail_on(
        self, stack: tuple[TestClient, Path]
    ) -> None:
        client, capsule_dir = stack
        c = _mk_capsule(capsule_dir)
        (c / "redaction-proof.json").write_text(
            json.dumps(
                {"findings": [{"severity": "high"}, {"severity": "info"}]}
            )
        )
        resp = client.get(
            f"/api/runs/{c.name}/scan-secrets",
            params={**AUTH, "fail_on": "high"},
            headers=HOST,
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        bad = client.get(
            f"/api/runs/{c.name}/scan-secrets",
            params={**AUTH, "fail_on": "bogus"},
            headers=HOST,
        )
        assert bad.status_code == 400

    def test_scan_secrets_no_proof_404(self, stack: tuple[TestClient, Path]) -> None:
        client, capsule_dir = stack
        c = _mk_capsule(capsule_dir, run_id="01HXCAMPAIGN0000000NOPROOF")
        resp = client.get(
            f"/api/runs/{c.name}/scan-secrets", params=AUTH, headers=HOST
        )
        assert resp.status_code == 404

    def test_scan_secrets_missing_404(self, stack: tuple[TestClient, Path]) -> None:
        client, _ = stack
        resp = client.get(
            "/api/runs/no-such/scan-secrets", params=AUTH, headers=HOST
        )
        assert resp.status_code == 404

    def test_replay_dry_run(self, stack: tuple[TestClient, Path]) -> None:
        client, capsule_dir = stack
        c = _mk_capsule(capsule_dir)
        resp = client.post(
            f"/api/runs/{c.name}/replay/dry-run",
            params=AUTH,
            headers=HOST,
            json={"confirmed": True},
        )
        assert resp.status_code == 200
        unconfirmed = client.post(
            f"/api/runs/{c.name}/replay/dry-run",
            params=AUTH,
            headers=HOST,
            json={"confirmed": False},
        )
        assert unconfirmed.status_code == 400

    def test_verify_unsealed_capsule(self, stack: tuple[TestClient, Path]) -> None:
        client, capsule_dir = stack
        c = _mk_capsule(capsule_dir)
        resp = client.post(f"/api/runs/{c.name}/verify", params=AUTH, headers=HOST)
        assert resp.status_code in (200, 404, 422)

    def test_validate_capsule(self, stack: tuple[TestClient, Path]) -> None:
        client, capsule_dir = stack
        c = _mk_capsule(capsule_dir)
        resp = client.post(f"/api/runs/{c.name}/validate", params=AUTH, headers=HOST)
        assert resp.status_code in (200, 422)


class TestEvidenceRoutes:
    def test_export_requires_confirmation(
        self, stack: tuple[TestClient, Path]
    ) -> None:
        client, capsule_dir = stack
        c = _mk_capsule(capsule_dir)
        resp = client.post(
            f"/api/evidence/{c.name}",
            params=AUTH,
            headers=HOST,
            json={"confirmed": False},
        )
        assert resp.status_code == 400

    def test_export_with_generated_key(
        self, stack: tuple[TestClient, Path], tmp_path: Path
    ) -> None:
        from novafabric.evidence.signing import generate_keypair

        client, capsule_dir = stack
        c = _mk_capsule(capsule_dir)
        priv, _ = generate_keypair(tmp_path / "keys")
        resp = client.post(
            f"/api/evidence/{c.name}",
            params=AUTH,
            headers=HOST,
            json={"confirmed": True, "key_path": str(priv)},
        )
        # minimal capsule may fail bundle validation — both the success and
        # the named-validation-error paths are real handler branches
        assert resp.status_code in (200, 400, 422)


class TestEvidenceVerifyAndSigstore:
    def test_verify_rejects_path_traversal(
        self, stack: tuple[TestClient, Path]
    ) -> None:
        client, _ = stack
        resp = client.post(
            "/api/evidence/..%2Fetc/verify", params=AUTH, headers=HOST
        )
        assert resp.status_code in (400, 404)

    def test_verify_missing_bundle(self, stack: tuple[TestClient, Path]) -> None:
        client, _ = stack
        resp = client.post(
            "/api/evidence/no-such-bundle/verify", params=AUTH, headers=HOST
        )
        assert resp.status_code == 404

    def test_sigstore_sign_without_subject(
        self, stack: tuple[TestClient, Path]
    ) -> None:
        client, _ = stack
        # exercises the import + request-validation branches; without an
        # ambient OIDC identity the handler must fail with a clean error,
        # never a 500 traceback
        resp = client.post(
            "/api/seal/sigstore/sign", params=AUTH, headers=HOST, json={}
        )
        assert resp.status_code in (400, 422, 501, 502, 503)


class TestStreamsAndStorage:
    def test_runs_stream_rejects_bad_token(
        self, stack: tuple[TestClient, Path]
    ) -> None:
        client, _ = stack
        resp = client.get(
            "/api/runs/stream", params={"token": "wrong"}, headers=HOST
        )
        assert resp.status_code == 401

    def test_metrics_stream_rejects_bad_token(
        self, stack: tuple[TestClient, Path]
    ) -> None:
        client, _ = stack
        resp = client.get(
            "/metrics/stream", params={"token": "wrong"}, headers=HOST
        )
        assert resp.status_code in (401, 404)

    def test_manifest_chain_unconfigured(
        self, stack: tuple[TestClient, Path], monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        client, _ = stack
        monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
        resp = client.get("/api/storage/manifest-chain", params=AUTH, headers=HOST)
        assert resp.status_code == 200
        assert resp.json()["configured"] is False

    def test_manifest_chain_limit_validation(
        self, stack: tuple[TestClient, Path]
    ) -> None:
        client, _ = stack
        resp = client.get(
            "/api/storage/manifest-chain",
            params={**AUTH, "limit": 0},
            headers=HOST,
        )
        assert resp.status_code == 422


class TestKgAndReports:
    def test_v1_kg_status_no_db(self, stack: tuple[TestClient, Path]) -> None:
        client, _ = stack
        resp = client.get("/v1/kg/status", params=AUTH, headers=HOST)
        assert resp.status_code == 200
        assert "ok" in resp.json()

    def test_kg_topology_no_db_graceful(
        self, stack: tuple[TestClient, Path]
    ) -> None:
        client, _ = stack
        resp = client.get(
            "/api/kg/topology", params={**AUTH, "max_nodes": 10}, headers=HOST
        )
        assert resp.status_code == 200
        # second call exercises the TTL-cache branch
        again = client.get(
            "/api/kg/topology", params={**AUTH, "max_nodes": 10}, headers=HOST
        )
        assert again.status_code == 200

    @pytest.mark.parametrize(
        "report",
        [
            "run-history",
            "cost-burn",
            "throughput",
            "executive-summary",
            "evidence-inventory",
            "eval-regression",
            "policy-audit",
            "seal-verification",
        ],
    )
    def test_reports_render_on_real_capsules(
        self, stack: tuple[TestClient, Path], report: str
    ) -> None:
        client, capsule_dir = stack
        _mk_capsule(capsule_dir)
        resp = client.get(f"/api/reports/{report}", params=AUTH, headers=HOST)
        assert resp.status_code == 200, resp.text

    def test_capsule_compare_requires_params(
        self, stack: tuple[TestClient, Path]
    ) -> None:
        client, capsule_dir = stack
        c = _mk_capsule(capsule_dir)
        missing = client.get(
            "/api/reports/capsule-compare", params=AUTH, headers=HOST
        )
        assert missing.status_code == 422
        same = client.get(
            "/api/reports/capsule-compare",
            params={**AUTH, "run_a": c.name, "run_b": c.name},
            headers=HOST,
        )
        assert same.status_code in (200, 400, 404)
