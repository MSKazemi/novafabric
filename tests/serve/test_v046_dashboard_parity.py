# Copyright 2024 NovaFabric Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Tests for the v0.46.0 dashboard parity gap-closure routes.

Covers 12 CLI capabilities that previously had no dashboard equivalent:

- GET  /api/eval/suites                     — nova eval list
- POST /api/eval/run                        — nova eval run
- GET  /api/policy/list                     — nova policy list
- POST /api/policy/sign                     — nova policy sign
- GET  /api/governance/vocabularies         — nova classify list-vocabularies
- POST /api/governance/classify-manual      — nova classify run
- POST /api/aibom/generate                  — nova aibom generate [--all]
- POST /api/ingest-capsule                  — nova ingest-capsule
- GET  /api/runs/{run_id}/tree              — nova run show --with-children
- GET  /api/runs/{run_id}/run-lineage       — nova run lineage
- GET  /api/lineage-store/profile           — nova lineage-store profile
- GET  /api/runs/{run_id}/scan-secrets      — nova scan-secrets
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

import pytest
import yaml

fastapi_installed = pytest.importorskip("fastapi")
pytest.importorskip("starlette")

from fastapi.testclient import TestClient  # noqa: E402

from novafabric.serve.app import create_app  # noqa: E402

VALID_TOKEN = "test-token-1234567890abcdef"
HEADERS = {"host": "127.0.0.1:4321"}
TOKEN_Q = f"token={VALID_TOKEN}"

RUN_ID = "01TEST00000000000000000001"
CHILD_ID = "01TEST00000000000000000002"
GRANDCHILD_ID = "01TEST00000000000000000003"

FIXTURE_KEYS = Path(__file__).parent.parent / "fixtures" / "promote" / "keys"


@pytest.fixture
def capsule_dir(tmp_path: Path) -> Path:
    base = tmp_path / "runs"
    base.mkdir()
    cdir = base / RUN_ID
    cdir.mkdir()
    manifest = {
        "schema_version": "0.1.0",
        "novafabric_version": "0.46.0",
        "run_id": RUN_ID,
        "created_at": "2026-06-11T00:00:00+00:00",
        "finished_at": "2026-06-11T00:00:01+00:00",
        "duration_ms": 1000,
        "command": ["python", "-c", "print(1)"],
        "exit_code": 0,
        "status": "success",
        "capture_mode": "cli-wrapper",
        "model_call_count": 0,
        "tool_call_count": 0,
        "mutating_tool_count": 0,
    }
    (cdir / "capsule.yaml").write_text(yaml.safe_dump(manifest))
    (cdir / "replay-policy.yaml").write_text("mode: forensic\n")
    (cdir / "trace.jsonl").write_text(
        json.dumps({"span_id": "root", "name": "test", "kind": "internal"}) + "\n"
    )
    (cdir / "model-calls.jsonl").write_text("")
    (cdir / "tool-calls.jsonl").write_text("")
    return base


@pytest.fixture
def client(capsule_dir: Path, tmp_path: Path) -> Iterator[TestClient]:
    app = create_app(
        token=VALID_TOKEN,
        capsule_dir=capsule_dir,
        db_path=tmp_path / "registry.db",
        static_dir=None,
    )
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# 1. GET /api/eval/suites — nova eval list
# ---------------------------------------------------------------------------


class TestEvalSuites:
    def test_lists_registered_suites(self, client: TestClient) -> None:
        resp = client.get(f"/api/eval/suites?{TOKEN_Q}", headers=HEADERS)
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        suite_ids = {s["suite_id"] for s in body["suites"]}
        assert "novafabric-smoke-v1" in suite_ids

    def test_suites_have_version_and_entry_point(self, client: TestClient) -> None:
        resp = client.get(f"/api/eval/suites?{TOKEN_Q}", headers=HEADERS)
        body = resp.json()
        for s in body["suites"]:
            assert "version" in s
            assert "entry_point" in s
            assert "oci_digest" in s

    def test_requires_token(self, client: TestClient) -> None:
        resp = client.get("/api/eval/suites", headers=HEADERS)
        assert resp.status_code in (401, 403)


# ---------------------------------------------------------------------------
# 2. POST /api/eval/run — nova eval run
# ---------------------------------------------------------------------------


class TestEvalRun:
    def test_smoke_suite_passes_on_valid_capsule(self, client: TestClient) -> None:
        resp = client.post(
            f"/api/eval/run?{TOKEN_Q}",
            headers=HEADERS,
            json={"run_id": RUN_ID, "suite": "novafabric-smoke-v1"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["result"]["suite_id"] == "novafabric-smoke-v1"
        assert body["result"]["passed"] is True

    def test_unknown_suite_returns_400(self, client: TestClient) -> None:
        resp = client.post(
            f"/api/eval/run?{TOKEN_Q}",
            headers=HEADERS,
            json={"run_id": RUN_ID, "suite": "no-such-suite"},
        )
        assert resp.status_code == 400

    def test_unknown_run_returns_404(self, client: TestClient) -> None:
        resp = client.post(
            f"/api/eval/run?{TOKEN_Q}",
            headers=HEADERS,
            json={"run_id": "01NOPE0000000000000000000X", "suite": "novafabric-smoke-v1"},
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 3. GET /api/policy/list — nova policy list
# ---------------------------------------------------------------------------


class TestPolicyList:
    def test_lists_rego_bundle_files(self, client: TestClient) -> None:
        resp = client.get(f"/api/policy/list?{TOKEN_Q}", headers=HEADERS)
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert isinstance(body["rego_files"], list)
        assert any(f["file"].endswith(".rego") for f in body["rego_files"])

    def test_signed_policies_empty_when_no_db(self, client: TestClient) -> None:
        resp = client.get(f"/api/policy/list?{TOKEN_Q}", headers=HEADERS)
        body = resp.json()
        assert isinstance(body["signed_policies"], list)


# ---------------------------------------------------------------------------
# 4. POST /api/policy/sign — nova policy sign
# ---------------------------------------------------------------------------


class TestPolicySign:
    def test_signs_and_stores_policy(self, client: TestClient, tmp_path: Path) -> None:
        db = tmp_path / "policy-sign.db"
        resp = client.post(
            f"/api/policy/sign?{TOKEN_Q}",
            headers=HEADERS,
            json={
                "key_path": str(FIXTURE_KEYS / "admin.pem"),
                "cert_path": str(FIXTURE_KEYS / "admin_cert.pem"),
                "proposer_subjects": ["alice"],
                "approver_subjects": ["bob", "carol"],
                "bypass_valid_hours": 24,
                "db_path": str(db),
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is True
        assert body["version"] >= 1
        assert body["proposers"] == ["alice"]

    def test_empty_proposers_returns_400(self, client: TestClient, tmp_path: Path) -> None:
        resp = client.post(
            f"/api/policy/sign?{TOKEN_Q}",
            headers=HEADERS,
            json={
                "key_path": str(FIXTURE_KEYS / "admin.pem"),
                "cert_path": str(FIXTURE_KEYS / "admin_cert.pem"),
                "proposer_subjects": [],
                "approver_subjects": ["bob"],
                "db_path": str(tmp_path / "x.db"),
            },
        )
        assert resp.status_code == 400

    def test_invalid_bypass_hours_returns_400(self, client: TestClient, tmp_path: Path) -> None:
        resp = client.post(
            f"/api/policy/sign?{TOKEN_Q}",
            headers=HEADERS,
            json={
                "key_path": str(FIXTURE_KEYS / "admin.pem"),
                "cert_path": str(FIXTURE_KEYS / "admin_cert.pem"),
                "proposer_subjects": ["alice"],
                "approver_subjects": ["bob"],
                "bypass_valid_hours": 1000,
                "db_path": str(tmp_path / "x.db"),
            },
        )
        assert resp.status_code == 400

    def test_missing_key_file_returns_400(self, client: TestClient, tmp_path: Path) -> None:
        resp = client.post(
            f"/api/policy/sign?{TOKEN_Q}",
            headers=HEADERS,
            json={
                "key_path": str(tmp_path / "nope.pem"),
                "cert_path": str(FIXTURE_KEYS / "admin_cert.pem"),
                "proposer_subjects": ["alice"],
                "approver_subjects": ["bob"],
                "db_path": str(tmp_path / "x.db"),
            },
        )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# 5. GET /api/governance/vocabularies — nova classify list-vocabularies
# ---------------------------------------------------------------------------


class TestGovernanceVocabularies:
    def test_lists_vocabularies(self, client: TestClient) -> None:
        resp = client.get(f"/api/governance/vocabularies?{TOKEN_Q}", headers=HEADERS)
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        frameworks = {v["framework"] for v in body["vocabularies"]}
        assert len(frameworks) >= 1
        for v in body["vocabularies"]:
            assert "version" in v
            assert "path" in v


# ---------------------------------------------------------------------------
# 6. POST /api/governance/classify-manual — nova classify run
# ---------------------------------------------------------------------------


class TestClassifyManual:
    def test_classifies_manual_record(self, client: TestClient) -> None:
        resp = client.post(
            f"/api/governance/classify-manual?{TOKEN_Q}",
            headers=HEADERS,
            json={
                "name": "loan-scorer",
                "description": "Scores consumer loan applications for creditworthiness",
                "use_case_domain": "finance",
                "deployment_context": "credit scoring of natural persons",
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is True
        assert "eu_ai_act_tier" in body["classification"]

    def test_missing_name_returns_422(self, client: TestClient) -> None:
        resp = client.post(
            f"/api/governance/classify-manual?{TOKEN_Q}",
            headers=HEADERS,
            json={"description": "x"},
        )
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 7. POST /api/aibom/generate — nova aibom generate [--all]
# ---------------------------------------------------------------------------


class TestAibomGenerate:
    def test_generate_single_capsule(self, client: TestClient, capsule_dir: Path) -> None:
        resp = client.post(
            f"/api/aibom/generate?{TOKEN_Q}",
            headers=HEADERS,
            json={"run_id": RUN_ID},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is True
        assert body["written"] == 1
        assert (capsule_dir / RUN_ID / "aibom.json").exists()

    def test_generate_skips_existing_without_force(
        self, client: TestClient, capsule_dir: Path
    ) -> None:
        (capsule_dir / RUN_ID / "aibom.json").write_text("{}")
        resp = client.post(
            f"/api/aibom/generate?{TOKEN_Q}",
            headers=HEADERS,
            json={"run_id": RUN_ID},
        )
        body = resp.json()
        assert body["written"] == 0
        assert body["skipped"] == 1

    def test_generate_force_overwrites(self, client: TestClient, capsule_dir: Path) -> None:
        (capsule_dir / RUN_ID / "aibom.json").write_text("{}")
        resp = client.post(
            f"/api/aibom/generate?{TOKEN_Q}",
            headers=HEADERS,
            json={"run_id": RUN_ID, "force": True},
        )
        body = resp.json()
        assert body["written"] == 1
        regenerated = json.loads((capsule_dir / RUN_ID / "aibom.json").read_text())
        assert regenerated != {}

    def test_generate_all(self, client: TestClient, capsule_dir: Path) -> None:
        resp = client.post(
            f"/api/aibom/generate?{TOKEN_Q}",
            headers=HEADERS,
            json={"all": True},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["written"] == 1
        assert (capsule_dir / RUN_ID / "aibom.json").exists()

    def test_unknown_run_returns_404(self, client: TestClient) -> None:
        resp = client.post(
            f"/api/aibom/generate?{TOKEN_Q}",
            headers=HEADERS,
            json={"run_id": "01NOPE0000000000000000000X"},
        )
        assert resp.status_code == 404

    def test_neither_run_nor_all_returns_400(self, client: TestClient) -> None:
        resp = client.post(
            f"/api/aibom/generate?{TOKEN_Q}", headers=HEADERS, json={}
        )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# 8. POST /api/ingest-capsule — nova ingest-capsule
# ---------------------------------------------------------------------------


class TestIngestCapsule:
    def test_ingest_single(self, client: TestClient) -> None:
        resp = client.post(
            f"/api/ingest-capsule?{TOKEN_Q}",
            headers=HEADERS,
            json={"run_id": RUN_ID},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is True
        assert body["found"] is True

    def test_ingest_all(self, client: TestClient) -> None:
        resp = client.post(
            f"/api/ingest-capsule?{TOKEN_Q}", headers=HEADERS, json={"all": True}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["indexed"] >= 1

    def test_ingest_unknown_run_returns_404(self, client: TestClient) -> None:
        resp = client.post(
            f"/api/ingest-capsule?{TOKEN_Q}",
            headers=HEADERS,
            json={"run_id": "01NOPE0000000000000000000X"},
        )
        assert resp.status_code == 404

    def test_neither_run_nor_all_returns_400(self, client: TestClient) -> None:
        resp = client.post(f"/api/ingest-capsule?{TOKEN_Q}", headers=HEADERS, json={})
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# 9. GET /api/runs/{run_id}/tree — nova run show --with-children
# ---------------------------------------------------------------------------


def _write_phase3_capsule(
    base: Path, run_id: str, parent: str | None, role: str, status: str = "COMPLETE"
) -> None:
    cdir = base / run_id
    cdir.mkdir(exist_ok=True)
    (cdir / "capsule.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "global_run_id": run_id if parent is None else parent,
                "parent_run_id": parent,
                "capsule_role": role,
                "status": status,
            }
        )
    )


class TestRunTree:
    def test_tree_with_children(self, client: TestClient, capsule_dir: Path) -> None:
        _write_phase3_capsule(capsule_dir, RUN_ID, None, "PARENT")
        _write_phase3_capsule(capsule_dir, CHILD_ID, RUN_ID, "WORKER")
        _write_phase3_capsule(capsule_dir, GRANDCHILD_ID, CHILD_ID, "WORKER")
        resp = client.get(f"/api/runs/{RUN_ID}/tree?{TOKEN_Q}", headers=HEADERS)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is True
        assert body["root"]["run_id"] == RUN_ID
        assert body["total_nodes"] == 3
        child = body["root"]["children"][0]
        assert child["run_id"] == CHILD_ID
        assert child["children"][0]["run_id"] == GRANDCHILD_ID

    def test_tree_synthetic_root_for_unknown_run(self, client: TestClient) -> None:
        resp = client.get(
            f"/api/runs/01NOPE0000000000000000000X/tree?{TOKEN_Q}", headers=HEADERS
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["root"]["is_synthetic"] is True


# ---------------------------------------------------------------------------
# 10. GET /api/runs/{run_id}/run-lineage — nova run lineage
# ---------------------------------------------------------------------------


class TestRunLineage:
    def _write_lineage(self, base: Path) -> None:
        cdir = base / RUN_ID
        edges = [
            {"source_run_id": RUN_ID, "target_run_id": CHILD_ID, "edge_type": "contains"},
            {"source_run_id": RUN_ID, "target_run_id": GRANDCHILD_ID, "edge_type": "spawned"},
        ]
        (cdir / "lineage.jsonl").write_text(
            "\n".join(json.dumps(e) for e in edges) + "\n"
        )

    def test_lists_all_edges(self, client: TestClient, capsule_dir: Path) -> None:
        self._write_lineage(capsule_dir)
        resp = client.get(f"/api/runs/{RUN_ID}/run-lineage?{TOKEN_Q}", headers=HEADERS)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is True
        assert body["count"] == 2

    def test_edge_type_filter(self, client: TestClient, capsule_dir: Path) -> None:
        self._write_lineage(capsule_dir)
        resp = client.get(
            f"/api/runs/{RUN_ID}/run-lineage?edge_types=spawned&{TOKEN_Q}",
            headers=HEADERS,
        )
        body = resp.json()
        assert body["count"] == 1
        assert body["edges"][0]["edge_type"] == "spawned"

    def test_invalid_edge_type_returns_400(self, client: TestClient) -> None:
        resp = client.get(
            f"/api/runs/{RUN_ID}/run-lineage?edge_types=bogus&{TOKEN_Q}",
            headers=HEADERS,
        )
        assert resp.status_code == 400

    def test_no_edges_returns_empty(self, client: TestClient) -> None:
        resp = client.get(f"/api/runs/{RUN_ID}/run-lineage?{TOKEN_Q}", headers=HEADERS)
        assert resp.status_code == 200
        assert resp.json()["count"] == 0


# ---------------------------------------------------------------------------
# 11. GET /api/lineage-store/profile — nova lineage-store profile
# ---------------------------------------------------------------------------


class TestLineageStoreProfile:
    def test_kuzudb_vertical_default(self, client: TestClient) -> None:
        resp = client.get(f"/api/lineage-store/profile?{TOKEN_Q}", headers=HEADERS)
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["target"] == "kuzudb-vertical"
        assert "services" in body["profile_yaml"]

    def test_janusgraph_minimal(self, client: TestClient) -> None:
        resp = client.get(
            f"/api/lineage-store/profile?target=janusgraph-minimal&rf=1&{TOKEN_Q}",
            headers=HEADERS,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["target"] == "janusgraph-minimal"
        assert "janusgraph" in body["profile_yaml"].lower()

    def test_unknown_target_returns_400(self, client: TestClient) -> None:
        resp = client.get(
            f"/api/lineage-store/profile?target=bogus&{TOKEN_Q}", headers=HEADERS
        )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# 12. GET /api/runs/{run_id}/scan-secrets — nova scan-secrets
# ---------------------------------------------------------------------------


class TestScanSecrets:
    def _write_proof(self, base: Path, severities: list[str]) -> None:
        by_sev = {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0}
        findings = []
        for i, sev in enumerate(severities):
            by_sev[sev] += 1
            findings.append(
                {
                    "rule_id": f"rule-{i}",
                    "severity": sev,
                    "target_ref": f"trace.jsonl:{i}",
                    "redaction_strategy": "mask",
                }
            )
        proof = {
            "capsule_run_id": RUN_ID,
            "findings_count": {"total": len(findings), "by_severity": by_sev},
            "findings": findings,
        }
        (base / RUN_ID / "redaction-proof.json").write_text(json.dumps(proof))

    def test_returns_findings(self, client: TestClient, capsule_dir: Path) -> None:
        self._write_proof(capsule_dir, ["high", "low"])
        resp = client.get(f"/api/runs/{RUN_ID}/scan-secrets?{TOKEN_Q}", headers=HEADERS)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is True
        assert body["findings_count"]["total"] == 2
        assert body["triggered"] is False

    def test_fail_on_threshold_triggers(self, client: TestClient, capsule_dir: Path) -> None:
        self._write_proof(capsule_dir, ["critical", "low"])
        resp = client.get(
            f"/api/runs/{RUN_ID}/scan-secrets?fail_on=high&{TOKEN_Q}", headers=HEADERS
        )
        body = resp.json()
        assert body["triggered"] is True
        assert body["triggered_count"] == 1

    def test_fail_on_threshold_not_triggered(
        self, client: TestClient, capsule_dir: Path
    ) -> None:
        self._write_proof(capsule_dir, ["low", "info"])
        resp = client.get(
            f"/api/runs/{RUN_ID}/scan-secrets?fail_on=high&{TOKEN_Q}", headers=HEADERS
        )
        assert resp.json()["triggered"] is False

    def test_missing_proof_returns_404(self, client: TestClient) -> None:
        resp = client.get(f"/api/runs/{RUN_ID}/scan-secrets?{TOKEN_Q}", headers=HEADERS)
        assert resp.status_code == 404

    def test_invalid_fail_on_returns_400(self, client: TestClient, capsule_dir: Path) -> None:
        self._write_proof(capsule_dir, ["low"])
        resp = client.get(
            f"/api/runs/{RUN_ID}/scan-secrets?fail_on=bogus&{TOKEN_Q}", headers=HEADERS
        )
        assert resp.status_code == 400
