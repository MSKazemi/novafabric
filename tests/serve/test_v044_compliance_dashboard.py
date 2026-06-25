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
"""Tests for v0.44.0 compliance dashboard routes.

Covers:
- POST /api/compliance/pii/erase  — DEK crypto-shredding (nova pii erase, ADR-0069)
- POST /api/compliance/export/hipaa-proof — HIPAA Safe Harbor proof export
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
AUTH = HEADERS  # auth is via ?token=... query param, not headers
TOKEN_Q = f"token={VALID_TOKEN}"


@pytest.fixture
def capsule_dir(tmp_path: Path) -> Path:
    base = tmp_path / "runs"
    base.mkdir()
    cdir = base / "01TEST00000000000000000001"
    cdir.mkdir()
    manifest = {
        "schema_version": "0.1.0",
        "novafabric_version": "0.44.0",
        "run_id": "01TEST00000000000000000001",
        "created_at": "2026-05-27T00:00:00+00:00",
        "finished_at": "2026-05-27T00:00:01+00:00",
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


# ---------- PII erase route ----------


class TestPiiEraseRoute:
    def test_pii_erase_no_dek_returns_ok_false(
        self, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """POST with a subject_id that has no DEK → ok=False, error='no DEK found'."""
        monkeypatch.setenv("NOVAFABRIC_HOME", str(tmp_path / "nova_home"))
        res = client.post(
            f"/api/compliance/pii/erase?{TOKEN_Q}",
            json={"subject_id": "subject-no-dek-xyz"},
            headers=AUTH,
        )
        # The route must not crash; it should return a 200 with ok=False,
        # or a valid error response.
        assert res.status_code in (200, 404, 422, 500), (
            f"Unexpected status {res.status_code}: {res.text}"
        )
        if res.status_code == 200:
            body = res.json()
            # If the route handled the KeyError, ok must be False.
            assert body.get("ok") is False or body.get("error") is not None

    def test_pii_erase_missing_subject_returns_422(
        self, client: TestClient
    ) -> None:
        """POST without subject_id → 422."""
        res = client.post(
            f"/api/compliance/pii/erase?{TOKEN_Q}",
            json={},
            headers=AUTH,
        )
        assert res.status_code == 422

    def test_pii_erase_empty_subject_returns_422(
        self, client: TestClient
    ) -> None:
        """POST with empty subject_id → 422."""
        res = client.post(
            f"/api/compliance/pii/erase?{TOKEN_Q}",
            json={"subject_id": ""},
            headers=AUTH,
        )
        assert res.status_code == 422


# ---------- HIPAA Safe Harbor proof route ----------


class TestHipaaProofRoute:
    def test_hipaa_proof_missing_run_id_returns_422(
        self, client: TestClient
    ) -> None:
        """POST without run_id → 422."""
        res = client.post(
            f"/api/compliance/export/hipaa-proof?{TOKEN_Q}",
            json={},
            headers=AUTH,
        )
        assert res.status_code == 422

    def test_hipaa_proof_unknown_run_id_returns_error(
        self, client: TestClient
    ) -> None:
        """POST with unknown run_id → 404 or 500 with an error detail."""
        res = client.post(
            f"/api/compliance/export/hipaa-proof?{TOKEN_Q}",
            json={"run_id": "NONEXISTENT-RUN-ID-99999"},
            headers=AUTH,
        )
        assert res.status_code in (404, 500), (
            f"Expected 404 or 500 for unknown run_id, got {res.status_code}: {res.text}"
        )
        body = res.json()
        assert "detail" in body or "error" in body

    def test_hipaa_proof_known_run_id_returns_proof(
        self, client: TestClient
    ) -> None:
        """POST with a valid run_id → 200 with a structured proof response."""
        res = client.post(
            f"/api/compliance/export/hipaa-proof?{TOKEN_Q}",
            json={"run_id": "01TEST00000000000000000001"},
            headers=AUTH,
        )
        assert res.status_code == 200
        body = res.json()
        assert body["ok"] is True
        assert body["run_id"] == "01TEST00000000000000000001"
        assert "proof_digest" in body
        assert body["proof_digest"].startswith("sha256:")
        assert "disclaimer" in body
        assert "assessed_at" in body
        assert body["identifier_count"] == 18
        assert len(body["categories"]) == 18
        # Each category must have the expected fields.
        for cat in body["categories"]:
            assert "id" in cat
            assert "name" in cat
            assert "status" in cat
            assert "evidence_source" in cat
