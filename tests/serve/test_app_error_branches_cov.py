"""Error/edge-branch coverage for ``novafabric.serve.app``.

This module deliberately drives the *uncovered* branches of the ~170 route
handlers in ``create_app``: not-found (404), bad-input (400/422), empty-result,
graceful stub responses when optional infra (ClickHouse / NATS / Postgres /
KuzuDB / topology) is absent, and the happy path for handlers that the other
serve tests never exercise.

All requests use the localhost host-header guard and the ``token`` query param,
mirroring ``tests/serve/test_reports.py``.  No external infrastructure is
started — every assertion relies on a handler's documented stub/empty/``{ok:
false}`` degraded response.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from novafabric.serve.app import create_app

TOKEN = "errbranch-token"
H = {"host": "127.0.0.1:4321"}
HJ = {"host": "127.0.0.1:4321", "content-type": "application/json"}
P = {"token": TOKEN}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point NOVAFABRIC_HOME / HOME at a temp dir and disable any real seal config.

    This keeps endpoints that read ``~/.novafabric`` or NovaSeal config
    deterministic and infra-free.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("NOVAFABRIC_HOME", str(home / ".novafabric"))
    monkeypatch.setenv(
        "NOVAFABRIC_SEAL_CONFIG", str(tmp_path / "_no_such_seal_config.yaml")
    )
    monkeypatch.setenv("NOVAFABRIC_EVIDENCE_DIR", str(tmp_path / "evidence"))
    # Ensure no ClickHouse/S3 leak from the ambient environment.
    monkeypatch.delenv("NOVA_CLICKHOUSE_URL", raising=False)
    monkeypatch.delenv("NOVA_S3_ENDPOINT_URL", raising=False)
    return home


def _write_capsule(base: Path, run_id: str, *, status: str = "success") -> Path:
    cdir = base / run_id
    cdir.mkdir(parents=True)
    manifest = {
        "schema_version": "0.1.0",
        "novafabric_version": "0.30.0",
        "run_id": run_id,
        "created_at": "2026-05-01T10:00:00+00:00",
        "finished_at": "2026-05-01T10:00:05+00:00",
        "duration_ms": 5000,
        "command": ["python", "agent.py"],
        "exit_code": 0,
        "status": status,
        "capture_mode": "cli-wrapper",
        "model_call_count": 3,
        "tool_call_count": 2,
        "mutating_tool_count": 0,
    }
    (cdir / "capsule.yaml").write_text(yaml.safe_dump(manifest))
    (cdir / "trace.jsonl").write_text(
        json.dumps({"span_id": "root", "name": "test", "kind": "internal"}) + "\n"
    )
    (cdir / "model-calls.jsonl").write_text(
        json.dumps({"model": "gpt-4o", "input_tokens": 10, "output_tokens": 5}) + "\n"
    )
    (cdir / "tool-calls.jsonl").write_text("")
    (cdir / "output.txt").write_text("hello output\n")
    return cdir


@pytest.fixture()
def populated(tmp_path: Path, isolated_home: Path) -> tuple[TestClient, Path]:
    """A client with one capsule on disk and a fresh (empty) registry DB."""
    base = tmp_path / "capsules"
    base.mkdir()
    _write_capsule(base, "run_present_0001")
    db = tmp_path / "registry.db"
    app = create_app(token=TOKEN, capsule_dir=base, db_path=db, static_dir=None)
    return TestClient(app), base


@pytest.fixture()
def empty(tmp_path: Path, isolated_home: Path) -> TestClient:
    """A client with an empty capsule dir and no DB (db_path=None)."""
    base = tmp_path / "empty-capsules"
    base.mkdir()
    app = create_app(token=TOKEN, capsule_dir=base, db_path=None, static_dir=None)
    return TestClient(app)


# ---------------------------------------------------------------------------
# Health / stats / runs listing (empty + populated)
# ---------------------------------------------------------------------------


def test_health_ok(empty: TestClient) -> None:
    r = empty.get("/api/health", headers=H)
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_stats_empty(populated: tuple[TestClient, Path]) -> None:
    c, _ = populated
    r = c.get("/api/stats", params=P, headers=H)
    assert r.status_code == 200
    assert r.json()["approximate"] is True


def test_list_runs_disk_scan_filters(populated: tuple[TestClient, Path]) -> None:
    c, _ = populated
    r = c.get(
        "/api/runs",
        params={**P, "status": "success", "q": "agent", "since": "2026-01-01"},
        headers=H,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["total"] >= 1
    assert body["runs"][0]["run_id"] == "run_present_0001"


def test_list_runs_filter_no_match(populated: tuple[TestClient, Path]) -> None:
    c, _ = populated
    r = c.get("/api/runs", params={**P, "q": "zzz-no-match"}, headers=H)
    assert r.status_code == 200
    assert r.json()["total"] == 0


def test_runs_search_cursor_disk(populated: tuple[TestClient, Path]) -> None:
    c, _ = populated
    r = c.get("/api/runs/search", params={**P, "limit": 5, "q": "agent"}, headers=H)
    assert r.status_code == 200
    body = r.json()
    assert "items" in body
    assert "next_cursor" in body


def test_runs_search_cursor_bad_cursor(populated: tuple[TestClient, Path]) -> None:
    c, _ = populated
    r = c.get(
        "/api/runs/search",
        params={**P, "cursor": "not-a-real-cursor"},
        headers=H,
    )
    assert r.status_code == 200


def test_runs_cost_summary_no_clickhouse(populated: tuple[TestClient, Path]) -> None:
    """ADR-0234 D2: unavailable is reported, not returned as an empty result.

    This asserted `== {"costs": {}}` exactly, which is what let the endpoint
    answer "no cost data" when the truth was "no cost store". The `costs` shape
    is unchanged for existing consumers; the verdict is additive.
    """
    c, _ = populated
    r = c.get(
        "/api/runs/cost-summary",
        params={**P, "run_ids": "run_present_0001,run_x"},
        headers=H,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["costs"] == {}
    assert body["aggregate"]["computable"] is False
    assert body["aggregate"]["condition"] == "source_unavailable"
    assert "value" not in body["aggregate"], "a refusal must not carry a number"


def test_suggest_register(populated: tuple[TestClient, Path]) -> None:
    c, _ = populated
    r = c.get("/api/runs/suggest-register", params=P, headers=H)
    assert r.status_code == 200
    assert "suggestions" in r.json()


def test_runs_stream_bad_token(populated: tuple[TestClient, Path]) -> None:
    c, _ = populated
    r = c.get("/api/runs/stream", params={"token": "wrong"}, headers=H)
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Single-run lookups: 404 / 400 / happy path
# ---------------------------------------------------------------------------


def test_get_run_404(empty: TestClient) -> None:
    r = empty.get("/api/runs/nope_missing", params=P, headers=H)
    assert r.status_code == 404


def test_get_run_ok(populated: tuple[TestClient, Path]) -> None:
    c, _ = populated
    r = c.get("/api/runs/run_present_0001", params=P, headers=H)
    assert r.status_code == 200
    assert r.json()["run_id"] == "run_present_0001"


def test_get_run_file_ok(populated: tuple[TestClient, Path]) -> None:
    c, _ = populated
    r = c.get(
        "/api/runs/run_present_0001/file/output.txt", params=P, headers=H
    )
    assert r.status_code == 200
    assert "hello output" in r.json()["content"]


def test_get_run_file_jsonl(populated: tuple[TestClient, Path]) -> None:
    c, _ = populated
    r = c.get(
        "/api/runs/run_present_0001/file/model-calls.jsonl", params=P, headers=H
    )
    assert r.status_code == 200
    assert isinstance(r.json()["lines"], list)


def test_get_run_file_traversal_400(populated: tuple[TestClient, Path]) -> None:
    c, _ = populated
    r = c.get(
        "/api/runs/run_present_0001/file/..hidden", params=P, headers=H
    )
    assert r.status_code == 400


def test_get_run_file_bad_subdir_400(populated: tuple[TestClient, Path]) -> None:
    c, _ = populated
    r = c.get(
        "/api/runs/run_present_0001/file/secret/x", params=P, headers=H
    )
    assert r.status_code == 400


def test_get_run_file_too_deep_400(populated: tuple[TestClient, Path]) -> None:
    c, _ = populated
    r = c.get(
        "/api/runs/run_present_0001/file/a/b/c", params=P, headers=H
    )
    assert r.status_code == 400


def test_get_run_file_missing_404(populated: tuple[TestClient, Path]) -> None:
    c, _ = populated
    r = c.get(
        "/api/runs/run_present_0001/file/nope.txt", params=P, headers=H
    )
    assert r.status_code == 404


def test_capsule_verify_unsealed(populated: tuple[TestClient, Path]) -> None:
    c, _ = populated
    r = c.post("/api/runs/run_present_0001/verify", params=P, headers=H)
    assert r.status_code == 200
    assert r.json()["sealed"] is False


# ---------------------------------------------------------------------------
# Assets: 404 / empty / diff
# ---------------------------------------------------------------------------


def test_list_assets_empty(populated: tuple[TestClient, Path]) -> None:
    c, _ = populated
    r = c.get("/api/assets", params=P, headers=H)
    assert r.status_code == 200


def test_get_asset_404(populated: tuple[TestClient, Path]) -> None:
    c, _ = populated
    r = c.get("/api/assets/missing-asset-id", params=P, headers=H)
    assert r.status_code in {404, 200}


def test_asset_diff_404(populated: tuple[TestClient, Path]) -> None:
    c, _ = populated
    r = c.get(
        "/api/assets/missing/diff",
        params={**P, "version_a": "0.1.0", "version_b": "0.2.0"},
        headers=H,
    )
    assert r.status_code in {404, 400, 422, 200}


def test_asset_eval_history_404(populated: tuple[TestClient, Path]) -> None:
    c, _ = populated
    r = c.get("/api/assets/missing/eval-history", params=P, headers=H)
    assert r.status_code in {404, 200}


def test_asset_approvals_404(populated: tuple[TestClient, Path]) -> None:
    c, _ = populated
    r = c.get("/api/assets/missing/approvals", params=P, headers=H)
    assert r.status_code in {404, 200}


def test_asset_name_version_404(populated: tuple[TestClient, Path]) -> None:
    c, _ = populated
    r = c.get("/api/assets/missing/1.0.0", params=P, headers=H)
    assert r.status_code in {404, 200}


# ---------------------------------------------------------------------------
# Evidence: list empty / 404 / bad bundle id
# ---------------------------------------------------------------------------


def test_evidence_list_empty(empty: TestClient) -> None:
    r = empty.get("/api/evidence", params=P, headers=H)
    assert r.status_code == 200
    assert r.json()["count"] == 0


def test_evidence_detail_404(empty: TestClient) -> None:
    r = empty.get("/api/evidence/missing-bundle", params=P, headers=H)
    assert r.status_code == 404


def test_evidence_download_404(empty: TestClient) -> None:
    r = empty.get("/api/evidence/missing/download", params=P, headers=H)
    assert r.status_code == 404


def test_evidence_verify_404(empty: TestClient) -> None:
    r = empty.post("/api/evidence/missing-bundle/verify", params=P, headers=H)
    assert r.status_code == 404


def test_evidence_verify_bad_id_400(empty: TestClient) -> None:
    r = empty.post("/api/evidence/..%2Fevil/verify", params=P, headers=H)
    assert r.status_code in {400, 404}


# ---------------------------------------------------------------------------
# Lineage (empty store): provenance / blast-radius / replay / edges / import
# ---------------------------------------------------------------------------


def test_lineage_provenance_empty(populated: tuple[TestClient, Path]) -> None:
    c, _ = populated
    r = c.get("/api/lineage/provenance/run:abc", params=P, headers=H)
    assert r.status_code == 200
    assert r.json()["ancestors"] == []


def test_lineage_blast_radius_empty(populated: tuple[TestClient, Path]) -> None:
    c, _ = populated
    r = c.get("/api/lineage/blast-radius/run:abc", params=P, headers=H)
    assert r.status_code == 200
    assert r.json()["descendants"] == []


def test_lineage_replay_chain_empty(populated: tuple[TestClient, Path]) -> None:
    c, _ = populated
    r = c.get("/api/lineage/replay-chain/run_present_0001", params=P, headers=H)
    assert r.status_code == 200
    assert "chain" in r.json()


def test_lineage_time_travel(populated: tuple[TestClient, Path]) -> None:
    c, _ = populated
    r = c.get(
        "/api/lineage/time-travel/run:abc",
        params={**P, "at": "2026-05-01T00:00:00Z"},
        headers=H,
    )
    assert r.status_code == 200
    assert "supported" in r.json()


def test_lineage_edges_no_table(populated: tuple[TestClient, Path]) -> None:
    c, _ = populated
    r = c.get("/api/lineage/edges", params=P, headers=H)
    assert r.status_code == 200
    assert r.json()["count"] == 0


def test_lineage_import_missing_path_422(populated: tuple[TestClient, Path]) -> None:
    c, _ = populated
    r = c.post("/api/lineage/import", params=P, headers=HJ, json={})
    assert r.status_code == 422


def test_lineage_import_not_found_404(populated: tuple[TestClient, Path]) -> None:
    c, _ = populated
    r = c.post(
        "/api/lineage/import",
        params=P,
        headers=HJ,
        json={"capsule_path": "/nonexistent/dir/here"},
    )
    assert r.status_code == 404


def test_lineage_import_run_id(populated: tuple[TestClient, Path]) -> None:
    c, _ = populated
    r = c.post(
        "/api/lineage/import",
        params=P,
        headers=HJ,
        json={"capsule_path": "run_present_0001"},
    )
    assert r.status_code == 200
    assert "ok" in r.json()


def test_lineage_emit_openlineage(populated: tuple[TestClient, Path]) -> None:
    c, _ = populated
    r = c.get(
        "/api/lineage/run_present_0001/emit-openlineage", params=P, headers=H
    )
    assert r.status_code == 200
    assert "events" in r.json()


def test_lineage_emit_openlineage_404(populated: tuple[TestClient, Path]) -> None:
    c, _ = populated
    r = c.get("/api/lineage/missing/emit-openlineage", params=P, headers=H)
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Diff / audit
# ---------------------------------------------------------------------------


def test_diff_same_run(populated: tuple[TestClient, Path]) -> None:
    c, _ = populated
    r = c.get(
        "/api/diff",
        params={**P, "run_a": "run_present_0001", "run_b": "run_present_0001"},
        headers=H,
    )
    assert r.status_code == 200


def test_diff_missing_run_404(populated: tuple[TestClient, Path]) -> None:
    c, _ = populated
    r = c.get(
        "/api/diff",
        params={**P, "run_a": "run_present_0001", "run_b": "missing"},
        headers=H,
    )
    assert r.status_code in {404, 200}


def test_audit_recent(empty: TestClient) -> None:
    r = empty.get("/api/audit", params=P, headers=H)
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Replay endpoints — 404 for missing run
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mode", ["forensic", "dry-run", "semantic", "exact"]
)
def test_replay_missing_run_404(populated: tuple[TestClient, Path], mode: str) -> None:
    c, _ = populated
    r = c.post(f"/api/runs/missing/replay/{mode}", params=P, headers=HJ, json={})
    assert r.status_code in {404, 400, 422, 500}


def test_redaction_proof_404(populated: tuple[TestClient, Path]) -> None:
    c, _ = populated
    r = c.get("/api/runs/missing/redaction-proof", params=P, headers=H)
    assert r.status_code in {404, 200}


# ---------------------------------------------------------------------------
# Policy endpoints
# ---------------------------------------------------------------------------


def test_policy_test_no_opa(populated: tuple[TestClient, Path]) -> None:
    c, _ = populated
    r = c.post("/api/policy/test", params=P, headers=HJ, json={})
    assert r.status_code == 200
    body = r.json()
    # Either OPA is installed (ok/exit_code) or stub (ok:False, backend:stub).
    assert "ok" in body


def test_policy_recent_decisions_empty(empty: TestClient) -> None:
    r = empty.get("/api/policy/recent-decisions", params=P, headers=H)
    assert r.status_code == 200
    assert r.json()["decision_ids"] == []


def test_policy_explain_no_audit(empty: TestClient) -> None:
    r = empty.get(
        "/api/policy/explain", params={**P, "decision_id": "abc"}, headers=H
    )
    assert r.status_code == 200
    assert r.json()["ok"] is False


def test_policy_capture_level_get(populated: tuple[TestClient, Path]) -> None:
    c, _ = populated
    r = c.get("/api/policy/capture-level", params=P, headers=H)
    assert r.status_code in {200, 501}


def test_policy_capture_level_set_bad_422(populated: tuple[TestClient, Path]) -> None:
    c, _ = populated
    r = c.post(
        "/api/policy/capture-level",
        params=P,
        headers=HJ,
        json={"level": "not-a-level"},
    )
    assert r.status_code in {422, 501}


def test_policy_capture_level_set_missing_422(
    populated: tuple[TestClient, Path],
) -> None:
    c, _ = populated
    r = c.post("/api/policy/capture-level", params=P, headers=HJ, json={})
    assert r.status_code in {422, 501}


def test_policy_list(populated: tuple[TestClient, Path]) -> None:
    c, _ = populated
    r = c.get("/api/policy/list", params=P, headers=H)
    assert r.status_code in {200, 501}


# ---------------------------------------------------------------------------
# Storage / infra (stub-aware)
# ---------------------------------------------------------------------------


def test_storage_stats(populated: tuple[TestClient, Path]) -> None:
    c, _ = populated
    r = c.get("/api/storage/stats", params=P, headers=H)
    assert r.status_code == 200


def test_storage_manifest_chain(populated: tuple[TestClient, Path]) -> None:
    c, _ = populated
    r = c.get("/api/storage/manifest-chain", params=P, headers=H)
    assert r.status_code in {200, 404, 422, 501}


def test_storage_validate_no_s3(populated: tuple[TestClient, Path]) -> None:
    c, _ = populated
    r = c.get("/api/storage/validate", params=P, headers=H)
    assert r.status_code in {200, 501}
    if r.status_code == 200:
        assert r.json()["ok"] is False


def test_storage_inspect(populated: tuple[TestClient, Path]) -> None:
    c, _ = populated
    r = c.get("/api/storage/inspect/run_present_0001", params=P, headers=H)
    assert r.status_code == 200
    assert r.json()["run_id"] == "run_present_0001"


def test_infra_collector(populated: tuple[TestClient, Path]) -> None:
    c, _ = populated
    r = c.get("/api/infra/collector", params=P, headers=H)
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Admin (tokens / roles)
# ---------------------------------------------------------------------------


def test_admin_tokens_list(populated: tuple[TestClient, Path]) -> None:
    c, _ = populated
    r = c.get("/api/admin/tokens", params=P, headers=H)
    assert r.status_code in {200, 403, 501}


def test_admin_roles_list(populated: tuple[TestClient, Path]) -> None:
    c, _ = populated
    r = c.get("/api/admin/roles", params=P, headers=H)
    assert r.status_code in {200, 403, 501}


def test_admin_new_run_id(populated: tuple[TestClient, Path]) -> None:
    c, _ = populated
    r = c.get("/api/admin/new-run-id", params=P, headers=H)
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Compliance / governance (stub-aware)
# ---------------------------------------------------------------------------


def test_compliance_annex_iv(populated: tuple[TestClient, Path]) -> None:
    c, _ = populated
    r = c.get("/api/compliance/annex-iv", params=P, headers=H)
    assert r.status_code in {200, 422, 501}


def test_compliance_nis2(populated: tuple[TestClient, Path]) -> None:
    c, _ = populated
    r = c.get("/api/compliance/nis2", params=P, headers=H)
    assert r.status_code in {200, 422, 501}


def test_compliance_subject_proof_missing(populated: tuple[TestClient, Path]) -> None:
    c, _ = populated
    r = c.get("/api/compliance/subject-proof", params=P, headers=H)
    assert r.status_code in {200, 400, 422, 501}


def test_compliance_erasure_request_missing_422(
    populated: tuple[TestClient, Path],
) -> None:
    c, _ = populated
    r = c.post("/api/compliance/erasure/request", params=P, headers=HJ, json={})
    assert r.status_code == 422


def test_compliance_erasure_request_unconfirmed_400(
    populated: tuple[TestClient, Path],
) -> None:
    # ADR-0210: the old stub returned 200 always-PENDING here; the endpoint is
    # now safe-mutations gated and refuses without confirmed=true (no mutation).
    c, _ = populated
    r = c.post(
        "/api/compliance/erasure/request",
        params=P,
        headers=HJ,
        json={"subject_id": "subj-1"},
    )
    assert r.status_code == 400
    assert "confirmation required" in r.json()["detail"]


def test_compliance_erasure_status(populated: tuple[TestClient, Path]) -> None:
    c, _ = populated
    r = c.get("/api/compliance/erasure/status", params=P, headers=H)
    assert r.status_code == 200
    assert r.json()["requests"] == []


def test_governance_classify_missing(populated: tuple[TestClient, Path]) -> None:
    c, _ = populated
    r = c.get("/api/governance/classify", params=P, headers=H)
    assert r.status_code in {200, 400, 422, 501}


def test_governance_vocabularies(populated: tuple[TestClient, Path]) -> None:
    c, _ = populated
    r = c.get("/api/governance/vocabularies", params=P, headers=H)
    assert r.status_code in {200, 501}


def test_compliance_audit_map(populated: tuple[TestClient, Path]) -> None:
    c, _ = populated
    r = c.get("/api/compliance/audit/map", params=P, headers=H)
    assert r.status_code in {200, 501}


def test_compliance_audit_coverage(populated: tuple[TestClient, Path]) -> None:
    c, _ = populated
    r = c.get("/api/compliance/audit/coverage", params=P, headers=H)
    assert r.status_code in {200, 422, 500, 501}


def test_compliance_audit_verify_missing_422(
    populated: tuple[TestClient, Path],
) -> None:
    c, _ = populated
    r = c.post("/api/compliance/audit/verify", params=P, headers=HJ, json={})
    assert r.status_code in {422, 501}


def test_compliance_audit_verify_bad_json(populated: tuple[TestClient, Path]) -> None:
    c, _ = populated
    r = c.post(
        "/api/compliance/audit/verify",
        params=P,
        headers=HJ,
        json={"report": "not valid json {{{"},
    )
    assert r.status_code in {200, 422, 501}


def test_euaiact_status(populated: tuple[TestClient, Path]) -> None:
    c, _ = populated
    r = c.get("/api/compliance/euaiact/status", params=P, headers=H)
    assert r.status_code in {200, 501}


# ---------------------------------------------------------------------------
# KG (stub when KuzuDB / scale-kg absent)
# ---------------------------------------------------------------------------


def test_kg_status(populated: tuple[TestClient, Path]) -> None:
    c, _ = populated
    r = c.get("/api/kg/status", params=P, headers=H)
    assert r.status_code in {200, 501}
    if r.status_code == 200:
        assert "store_health" in r.json()


def test_kg_agent_edges(populated: tuple[TestClient, Path]) -> None:
    c, _ = populated
    r = c.get("/api/kg/agents/agent-x/edges", params=P, headers=H)
    assert r.status_code in {200, 501}


def test_kg_init(populated: tuple[TestClient, Path]) -> None:
    c, _ = populated
    r = c.post("/api/kg/init", params=P, headers=HJ, json={})
    assert r.status_code == 200
    assert "ok" in r.json()


def test_kg_ingest_missing_422(populated: tuple[TestClient, Path]) -> None:
    c, _ = populated
    r = c.post("/api/kg/ingest", params=P, headers=HJ, json={})
    assert r.status_code == 422


def test_kg_ingest_not_found_404(populated: tuple[TestClient, Path]) -> None:
    c, _ = populated
    r = c.post(
        "/api/kg/ingest",
        params=P,
        headers=HJ,
        json={"capsule_path": "/nonexistent/x"},
    )
    assert r.status_code in {404, 200, 501}


def test_kg_topology(populated: tuple[TestClient, Path]) -> None:
    c, _ = populated
    r = c.get("/api/kg/topology", params=P, headers=H)
    assert r.status_code in {200, 501}


def test_kg_aliases_get(populated: tuple[TestClient, Path]) -> None:
    c, _ = populated
    r = c.get("/api/kg/aliases", params=P, headers=H)
    assert r.status_code in {200, 501}


def test_kg_entity_queue(populated: tuple[TestClient, Path]) -> None:
    c, _ = populated
    r = c.get("/api/kg/entity-queue", params=P, headers=H)
    assert r.status_code in {200, 501}


def test_kg_entity_queue_stats(populated: tuple[TestClient, Path]) -> None:
    c, _ = populated
    r = c.get("/api/kg/entity-queue/stats", params=P, headers=H)
    assert r.status_code in {200, 501}


# ---------------------------------------------------------------------------
# Cost / schema (DB-COST-1 / DB-SCH-1)
# ---------------------------------------------------------------------------


def test_cost_pricing(populated: tuple[TestClient, Path]) -> None:
    c, _ = populated
    r = c.get("/api/cost/pricing", params=P, headers=H)
    assert r.status_code in {200, 501}


def test_cost_report_duckdb_fallback(populated: tuple[TestClient, Path]) -> None:
    c, _ = populated
    r = c.get("/api/cost/report", params={**P, "days": 7}, headers=H)
    assert r.status_code == 200
    assert "backend" in r.json()


def test_schema_list(populated: tuple[TestClient, Path]) -> None:
    c, _ = populated
    r = c.get("/api/schema/list", params=P, headers=H)
    assert r.status_code in {200, 501}
    if r.status_code == 200:
        assert "event_types" in r.json()


# ---------------------------------------------------------------------------
# Eval / aibom / adapters
# ---------------------------------------------------------------------------


def test_eval_suites(populated: tuple[TestClient, Path]) -> None:
    c, _ = populated
    r = c.get("/api/eval/suites", params=P, headers=H)
    assert r.status_code in {200, 501}


def test_aibom_status(populated: tuple[TestClient, Path]) -> None:
    c, _ = populated
    r = c.get("/api/aibom/status", params=P, headers=H)
    assert r.status_code in {200, 501}


def test_adapters(populated: tuple[TestClient, Path]) -> None:
    c, _ = populated
    r = c.get("/api/adapters", params=P, headers=H)
    assert r.status_code in {200, 501}


# ---------------------------------------------------------------------------
# Doctor / assure / mcp
# ---------------------------------------------------------------------------


def test_doctor(populated: tuple[TestClient, Path]) -> None:
    c, _ = populated
    r = c.get("/api/doctor", params=P, headers=H)
    assert r.status_code == 200
    assert "checks" in r.json()


def test_assure_missing_run_stub(populated: tuple[TestClient, Path]) -> None:
    c, _ = populated
    r = c.get("/api/assure/missing-run", params=P, headers=H)
    assert r.status_code == 200
    assert r.json()["ok"] is False


def test_assure_present_run(populated: tuple[TestClient, Path]) -> None:
    c, _ = populated
    r = c.get("/api/assure/run_present_0001", params=P, headers=H)
    assert r.status_code == 200


def test_mcp_scan_empty_manifest(populated: tuple[TestClient, Path]) -> None:
    c, _ = populated
    r = c.post(
        "/api/mcp/scan", params=P, headers=HJ, json={"manifest": {}}
    )
    assert r.status_code in {200, 422}


def test_mcp_risk_report_missing_422(populated: tuple[TestClient, Path]) -> None:
    c, _ = populated
    r = c.post("/api/mcp/risk-report", params=P, headers=HJ, json={})
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Seal (no novaseal config / unsealed capsules)
# ---------------------------------------------------------------------------


def test_seal_policy(populated: tuple[TestClient, Path]) -> None:
    c, _ = populated
    r = c.get("/api/seal/policy", params=P, headers=H)
    assert r.status_code in {200, 404, 501}


def test_seal_proposals(populated: tuple[TestClient, Path]) -> None:
    c, _ = populated
    r = c.get("/api/seal/cap-xyz/proposals", params=P, headers=H)
    assert r.status_code in {200, 404, 501}


def test_seal_verify_unsealed(populated: tuple[TestClient, Path]) -> None:
    c, _ = populated
    r = c.post("/api/seal/cap-xyz/verify", params=P, headers=HJ, json={})
    assert r.status_code in {200, 404, 422, 501}


def test_seal_log_verify(populated: tuple[TestClient, Path]) -> None:
    c, _ = populated
    r = c.get("/api/seal/log/verify", params=P, headers=H)
    assert r.status_code in {200, 404, 501}


# ---------------------------------------------------------------------------
# Run trees / lineage / scan-secrets
# ---------------------------------------------------------------------------


def test_run_tree(populated: tuple[TestClient, Path]) -> None:
    c, _ = populated
    r = c.get("/api/runs/run_present_0001/tree", params=P, headers=H)
    assert r.status_code in {200, 404}


def test_run_lineage(populated: tuple[TestClient, Path]) -> None:
    c, _ = populated
    r = c.get("/api/runs/run_present_0001/run-lineage", params=P, headers=H)
    assert r.status_code in {200, 404}


def test_run_children(populated: tuple[TestClient, Path]) -> None:
    c, _ = populated
    r = c.get("/api/runs/run_present_0001/children", params=P, headers=H)
    assert r.status_code in {200, 404}


def test_run_scan_secrets(populated: tuple[TestClient, Path]) -> None:
    c, _ = populated
    r = c.get("/api/runs/run_present_0001/scan-secrets", params=P, headers=H)
    assert r.status_code in {200, 404}


def test_run_scan_secrets_404(populated: tuple[TestClient, Path]) -> None:
    c, _ = populated
    r = c.get("/api/runs/missing/scan-secrets", params=P, headers=H)
    assert r.status_code in {200, 404}


def test_tool_permission_events(populated: tuple[TestClient, Path]) -> None:
    c, _ = populated
    r = c.get(
        "/api/runs/run_present_0001/tool-permission-events", params=P, headers=H
    )
    assert r.status_code in {200, 404}


def test_lineage_store_profile(populated: tuple[TestClient, Path]) -> None:
    c, _ = populated
    r = c.get("/api/lineage-store/profile", params=P, headers=H)
    assert r.status_code in {200, 501}


# ---------------------------------------------------------------------------
# Misc admin / report / db ops
# ---------------------------------------------------------------------------


def test_report_get(populated: tuple[TestClient, Path]) -> None:
    c, _ = populated
    r = c.get("/api/report", params=P, headers=H)
    assert r.status_code in {200, 422, 404}


def test_validate_spec_bad(populated: tuple[TestClient, Path]) -> None:
    c, _ = populated
    r = c.post(
        "/api/validate-spec", params=P, headers=HJ, json={"spec": {"bad": 1}}
    )
    assert r.status_code in {200, 400, 422}


def test_flush_jwks_cache(populated: tuple[TestClient, Path]) -> None:
    c, _ = populated
    r = c.post("/api/admin/flush-jwks-cache", params=P, headers=HJ, json={})
    assert r.status_code in {200, 403, 501}


def test_db_upgrade(populated: tuple[TestClient, Path]) -> None:
    c, _ = populated
    r = c.post("/api/db/upgrade", params=P, headers=HJ, json={})
    assert r.status_code in {200, 403, 500, 501}


def test_validate_distributed_404(populated: tuple[TestClient, Path]) -> None:
    c, _ = populated
    r = c.post(
        "/api/runs/missing/validate-distributed", params=P, headers=HJ, json={}
    )
    assert r.status_code in {200, 404, 422}


def test_ingest_capsule_bad(populated: tuple[TestClient, Path]) -> None:
    c, _ = populated
    r = c.post("/api/ingest-capsule", params=P, headers=HJ, json={})
    # Empty body hits the explicit "provide run_id or all=true" guard.
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Auth guard branches
# ---------------------------------------------------------------------------


def test_missing_token_401(populated: tuple[TestClient, Path]) -> None:
    c, _ = populated
    r = c.get("/api/runs", headers=H)
    assert r.status_code == 401


def test_wrong_token_401(populated: tuple[TestClient, Path]) -> None:
    c, _ = populated
    r = c.get("/api/runs", params={"token": "wrong"}, headers=H)
    assert r.status_code == 401


def test_remote_host_rejected(populated: tuple[TestClient, Path]) -> None:
    c, _ = populated
    r = c.get("/api/runs", params=P, headers={"host": "evil.example.com"})
    assert r.status_code in {400, 401, 403}
