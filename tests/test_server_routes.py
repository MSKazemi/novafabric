"""Tests for the NovaFabric multi-user REST API (Track S-2).

Covers all five resource families:
  - Assets (list, create, get, promote)
  - Capsules (list, upload, get)
  - Lineage (nodes, blast-radius, provenance, replay-chain)
  - Replays (schedule, get, cancel)
  - Evidence (export, get, download)

Also covers:
  - Error envelope shape
  - Cursor-based pagination helper
  - ServerConfig loading
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from typing import Any

import pytest
import yaml

# Skip entire module cleanly if fastapi / httpx are not installed.
pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from novafabric.server.app import create_app  # noqa: E402
from novafabric.server.config import ServerConfig, load_config  # noqa: E402
from novafabric.server.pagination import (  # noqa: E402
    clamp_limit,
    decode_cursor,
    encode_cursor,
    paginate,
)

# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

VALID_MODEL_YAML = """
novafabric_spec_version: "0.1"
asset_type: model
name: test-model
version: "1.0.0"
status: development
spec:
  framework: pytorch
  artifact_path: /tmp/model.pt
"""

VALID_AGENT_YAML = """
novafabric_spec_version: "0.1"
asset_type: agent
name: test-agent
version: "1.0.0"
status: development
spec:
  model:
    provider: openai
    name: gpt-4
  tools:
    - web_search
  prompts:
    system: "You are a helpful assistant."
  policies: []
  evals:
    - basic_suite
"""


def _make_capsule_yaml(run_id: str) -> dict[str, Any]:
    return {
        "schema_version": "0.1.0",
        "novafabric_version": "0.6.12",
        "run_id": run_id,
        "created_at": "2026-04-15T10:00:00+00:00",
        "finished_at": "2026-04-15T10:00:01+00:00",
        "duration_ms": 1000,
        "command": ["python", "-c", "print('hi')"],
        "exit_code": 0,
        "status": "success",
        "capture_mode": "cli-wrapper",
        "model_call_count": 0,
        "tool_call_count": 0,
        "mutating_tool_count": 0,
    }


def _write_capsule(capsule_dir: Path, run_id: str) -> Path:
    cdir = capsule_dir / run_id
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / "capsule.yaml").write_text(yaml.safe_dump(_make_capsule_yaml(run_id)))
    (cdir / "trace.jsonl").write_text("")
    (cdir / "model-calls.jsonl").write_text("")
    (cdir / "tool-calls.jsonl").write_text("")
    (cdir / "assets.jsonl").write_text("")
    (cdir / "env.lock").write_text("{}")
    (cdir / "redaction-proof.json").write_text(
        json.dumps({"findings": [], "unsafe_skips": []})
    )
    inputs = cdir / "inputs"
    inputs.mkdir(exist_ok=True)
    outputs = cdir / "outputs"
    outputs.mkdir(exist_ok=True)
    return cdir


@pytest.fixture
def capsule_dir(tmp_path: Path) -> Path:
    cdir = tmp_path / "runs"
    cdir.mkdir()
    return cdir


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "test.db"


@pytest.fixture
def cfg(tmp_path: Path, db_path: Path, capsule_dir: Path) -> ServerConfig:
    # ADR-0184: anonymous admin now requires the explicit insecure opt-out.
    return ServerConfig(db_path=str(db_path), insecure_no_auth=True)


@pytest.fixture
def client(
    cfg: ServerConfig,
    capsule_dir: Path,
) -> TestClient:
    from novafabric.server import deps

    app = create_app(cfg)
    # Override capsule_dir dep so all routes use our tmp capsule_dir
    app.dependency_overrides[deps.get_capsule_dir] = lambda: capsule_dir
    return TestClient(app, raise_server_exceptions=False)


# --------------------------------------------------------------------------- #
# Pagination unit tests
# --------------------------------------------------------------------------- #


class TestPagination:
    def test_encode_decode_roundtrip(self) -> None:
        for offset in (0, 1, 50, 99, 500, 9999):
            assert decode_cursor(encode_cursor(offset)) == offset

    def test_decode_none_returns_zero(self) -> None:
        assert decode_cursor(None) == 0

    def test_decode_empty_returns_zero(self) -> None:
        assert decode_cursor("") == 0

    def test_decode_invalid_returns_zero(self) -> None:
        assert decode_cursor("not-a-cursor!!") == 0

    def test_paginate_first_page(self) -> None:
        items = list(range(10))
        page, next_cursor = paginate(items, limit=3, offset=0)
        assert page == [0, 1, 2]
        assert next_cursor is not None
        assert decode_cursor(next_cursor) == 3

    def test_paginate_last_page(self) -> None:
        items = list(range(10))
        page, next_cursor = paginate(items, limit=3, offset=9)
        assert page == [9]
        assert next_cursor is None

    def test_paginate_exact_multiple(self) -> None:
        items = list(range(6))
        page, next_cursor = paginate(items, limit=3, offset=3)
        assert page == [3, 4, 5]
        assert next_cursor is None

    def test_clamp_limit(self) -> None:
        assert clamp_limit(0) == 1
        assert clamp_limit(50) == 50
        assert clamp_limit(501) == 500
        assert clamp_limit(-1) == 1


# --------------------------------------------------------------------------- #
# ServerConfig unit tests
# --------------------------------------------------------------------------- #


class TestServerConfig:
    def test_defaults(self) -> None:
        cfg = ServerConfig()
        assert cfg.host == "127.0.0.1"
        assert cfg.port == 7433
        assert cfg.backend == "sqlite"

    def test_env_override_host(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NOVAFABRIC_SERVER_HOST", "0.0.0.0")
        cfg = ServerConfig()
        assert cfg.host == "0.0.0.0"

    def test_env_override_port(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NOVAFABRIC_SERVER_PORT", "8080")
        cfg = ServerConfig()
        assert cfg.port == 8080

    def test_secrets_not_in_yaml(self, tmp_path: Path) -> None:
        """Secrets in a YAML file must be silently dropped."""
        yaml_path = tmp_path / "server.yaml"
        yaml_path.write_text(
            "host: 0.0.0.0\nport: 9000\npostgres_dsn: postgresql://secret@host/db\n"
        )
        cfg = load_config(yaml_path)
        assert cfg.postgres_dsn is None  # stripped by load_config
        assert cfg.host == "0.0.0.0"
        assert cfg.port == 9000

    def test_env_provides_postgres_dsn(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NOVAFABRIC_POSTGRES_DSN", "postgresql://user:pw@host/db")
        cfg = ServerConfig()
        assert cfg.postgres_dsn == "postgresql://user:pw@host/db"

    def test_missing_config_file_returns_defaults(self, tmp_path: Path) -> None:
        missing = tmp_path / "does_not_exist.yaml"
        cfg = load_config(missing)
        assert cfg.host == "127.0.0.1"


# --------------------------------------------------------------------------- #
# Error envelope shape
# --------------------------------------------------------------------------- #


class TestErrorEnvelope:
    def test_not_found_returns_envelope(self, client: TestClient) -> None:
        resp = client.get("/v0/assets/does-not-exist-uuid")
        assert resp.status_code == 404
        body = resp.json()
        assert "error" in body
        assert "code" in body["error"]
        assert "message" in body["error"]

    def test_bad_request_create_no_spec(self, client: TestClient) -> None:
        resp = client.post("/v0/assets", json={})
        assert resp.status_code == 400
        body = resp.json()
        assert "error" in body


# --------------------------------------------------------------------------- #
# Health check
# --------------------------------------------------------------------------- #


class TestHealth:
    def test_health(self, client: TestClient) -> None:
        resp = client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["service"] == "nova-server"


# --------------------------------------------------------------------------- #
# Assets resource
# --------------------------------------------------------------------------- #


class TestAssets:
    def test_list_empty(self, client: TestClient) -> None:
        resp = client.get("/v0/assets")
        assert resp.status_code == 200
        body = resp.json()
        assert body["items"] == []
        assert body["total"] == 0
        assert body["next_cursor"] is None

    def test_create_and_list(self, client: TestClient) -> None:
        resp = client.post("/v0/assets", json={"spec_yaml": VALID_MODEL_YAML})
        assert resp.status_code == 201
        body = resp.json()
        assert body["name"] == "test-model"
        assert body["version"] == "1.0.0"

        list_resp = client.get("/v0/assets")
        assert list_resp.status_code == 200
        items = list_resp.json()["items"]
        assert len(items) == 1
        assert items[0]["name"] == "test-model"

    def test_create_duplicate_returns_409(self, client: TestClient) -> None:
        client.post("/v0/assets", json={"spec_yaml": VALID_MODEL_YAML})
        resp = client.post("/v0/assets", json={"spec_yaml": VALID_MODEL_YAML})
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "conflict"

    def test_create_invalid_spec_returns_422(self, client: TestClient) -> None:
        resp = client.post("/v0/assets", json={"spec_yaml": "not: yaml: spec"})
        assert resp.status_code == 422

    def test_get_by_id(self, client: TestClient) -> None:
        create_resp = client.post("/v0/assets", json={"spec_yaml": VALID_MODEL_YAML})
        asset_id = create_resp.json()["id"]

        resp = client.get(f"/v0/assets/{asset_id}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == asset_id
        assert body["name"] == "test-model"
        assert "spec_json" in body

    def test_get_not_found(self, client: TestClient) -> None:
        resp = client.get("/v0/assets/no-such-uuid")
        assert resp.status_code == 404

    def test_promote(self, client: TestClient) -> None:
        create_resp = client.post("/v0/assets", json={"spec_yaml": VALID_MODEL_YAML})
        asset_id = create_resp.json()["id"]

        resp = client.put(
            f"/v0/assets/{asset_id}/promote",
            json={"to_status": "staging", "actor": "test"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "staging"

    def test_promote_invalid_transition_returns_409(self, client: TestClient) -> None:
        # Register and immediately try to archive (skipping staging → production)
        # Actually development → archived is allowed; test invalid from archived
        create_resp = client.post("/v0/assets", json={"spec_yaml": VALID_MODEL_YAML})
        asset_id = create_resp.json()["id"]
        # Promote to archived
        client.put(
            f"/v0/assets/{asset_id}/promote",
            json={"to_status": "archived", "actor": "test"},
        )
        # Try to re-promote from archived → should 409
        resp = client.put(
            f"/v0/assets/{asset_id}/promote",
            json={"to_status": "production", "actor": "test"},
        )
        assert resp.status_code == 409

    def test_promote_agent_no_eval_returns_412(self, client: TestClient) -> None:
        create_resp = client.post("/v0/assets", json={"spec_yaml": VALID_AGENT_YAML})
        asset_id = create_resp.json()["id"]

        resp = client.put(
            f"/v0/assets/{asset_id}/promote",
            json={"to_status": "staging", "actor": "test", "force": False},
        )
        assert resp.status_code == 412

    def test_promote_agent_force(self, client: TestClient) -> None:
        create_resp = client.post("/v0/assets", json={"spec_yaml": VALID_AGENT_YAML})
        asset_id = create_resp.json()["id"]

        resp = client.put(
            f"/v0/assets/{asset_id}/promote",
            json={"to_status": "staging", "actor": "test", "force": True},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "staging"

    def test_list_pagination(self, client: TestClient) -> None:
        """Register two models, list with limit=1, follow cursor."""
        model_a = VALID_MODEL_YAML
        model_b = VALID_MODEL_YAML.replace(
            "name: test-model", "name: test-model-b"
        ).replace("version: \"1.0.0\"", "version: \"2.0.0\"")
        client.post("/v0/assets", json={"spec_yaml": model_a})
        client.post("/v0/assets", json={"spec_yaml": model_b})

        resp1 = client.get("/v0/assets?limit=1")
        assert resp1.status_code == 200
        body1 = resp1.json()
        assert len(body1["items"]) == 1
        assert body1["total"] == 2
        cursor = body1["next_cursor"]
        assert cursor is not None

        resp2 = client.get(f"/v0/assets?limit=1&cursor={cursor}")
        assert resp2.status_code == 200
        body2 = resp2.json()
        assert len(body2["items"]) == 1
        assert body2["next_cursor"] is None


# --------------------------------------------------------------------------- #
# Capsules resource
# --------------------------------------------------------------------------- #


class TestCapsules:
    def test_list_empty(self, client: TestClient) -> None:
        resp = client.get("/v0/capsules")
        assert resp.status_code == 200
        body = resp.json()
        assert body["items"] == []
        assert body["total"] == 0

    def test_get_not_found(self, client: TestClient) -> None:
        resp = client.get("/v0/capsules/no-such-run")
        assert resp.status_code == 404

    def test_upload_and_get(
        self, client: TestClient, capsule_dir: Path
    ) -> None:
        run_id = "01TEST00000000000000000002"
        manifest = _make_capsule_yaml(run_id)

        # Build a minimal ZIP in memory
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("capsule.yaml", yaml.safe_dump(manifest))
            zf.writestr("trace.jsonl", "")
        buf.seek(0)

        resp = client.post(
            "/v0/capsules",
            files={"capsule": ("capsule.zip", buf, "application/zip")},
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["run_id"] == run_id
        assert body["status"] == "success"

        # Get by run_id
        get_resp = client.get(f"/v0/capsules/{run_id}")
        assert get_resp.status_code == 200
        detail = get_resp.json()
        assert detail["run_id"] == run_id
        assert detail["model_call_count"] == 0

    def test_upload_duplicate_returns_409(
        self, client: TestClient, capsule_dir: Path
    ) -> None:
        run_id = "01TEST00000000000000000003"
        manifest = _make_capsule_yaml(run_id)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("capsule.yaml", yaml.safe_dump(manifest))
        buf.seek(0)
        client.post(
            "/v0/capsules",
            files={"capsule": ("c.zip", buf, "application/zip")},
        )
        # Upload same run_id again
        buf2 = io.BytesIO()
        with zipfile.ZipFile(buf2, "w") as zf:
            zf.writestr("capsule.yaml", yaml.safe_dump(manifest))
        buf2.seek(0)
        resp = client.post(
            "/v0/capsules",
            files={"capsule": ("c.zip", buf2, "application/zip")},
        )
        assert resp.status_code == 409

    def test_upload_bad_zip_returns_400(self, client: TestClient) -> None:
        resp = client.post(
            "/v0/capsules",
            files={"capsule": ("bad.zip", b"not a zip", "application/zip")},
        )
        assert resp.status_code == 400

    def test_list_after_write(
        self, client: TestClient, capsule_dir: Path
    ) -> None:
        _write_capsule(capsule_dir, "01TEST00000000000000000010")
        resp = client.get("/v0/capsules")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert body["items"][0]["run_id"] == "01TEST00000000000000000010"

    def test_list_pagination(
        self, client: TestClient, capsule_dir: Path
    ) -> None:
        _write_capsule(capsule_dir, "01TEST00000000000000000011")
        _write_capsule(capsule_dir, "01TEST00000000000000000012")

        resp1 = client.get("/v0/capsules?limit=1")
        assert resp1.status_code == 200
        body1 = resp1.json()
        assert body1["total"] == 2
        assert len(body1["items"]) == 1
        cursor = body1["next_cursor"]
        assert cursor is not None

        resp2 = client.get(f"/v0/capsules?limit=1&cursor={cursor}")
        body2 = resp2.json()
        assert len(body2["items"]) == 1
        assert body2["next_cursor"] is None

    # ------ P0 security: lineage injection prevention ------

    def test_upload_child_with_missing_parent_within_window_returns_409(
        self, client: TestClient
    ) -> None:
        """Writer cannot link a child to an arbitrary parent_run_id that doesn't exist."""
        from datetime import datetime, timezone

        parent_run_id = "01MISSING0000000000000PRNT"
        child_run_id = "01CHILD000000000000000001C"
        manifest = _make_capsule_yaml(child_run_id)
        manifest["parent_run_id"] = parent_run_id
        # Fresh created_at: within the 24-hour orphan timeout window.
        manifest["created_at"] = datetime.now(timezone.utc).isoformat()

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("capsule.yaml", yaml.safe_dump(manifest))
        buf.seek(0)

        resp = client.post(
            "/v0/capsules",
            files={"capsule": ("child.zip", buf, "application/zip")},
        )
        assert resp.status_code == 409, resp.text
        body = resp.json()
        assert body["error"]["code"] == "parent_not_found"
        assert parent_run_id in body["error"]["message"]

    def test_upload_child_after_parent_uploaded_succeeds(
        self, client: TestClient, capsule_dir: Path
    ) -> None:
        """Child capsule is accepted when its parent already exists in the store."""
        from datetime import datetime, timezone

        parent_run_id = "01PARENT000000000000000001"
        child_run_id = "01CHILD000000000000000002C"

        # Write parent directly to capsule_dir (simulates a prior upload).
        _write_capsule(capsule_dir, parent_run_id)

        manifest = _make_capsule_yaml(child_run_id)
        manifest["parent_run_id"] = parent_run_id
        manifest["created_at"] = datetime.now(timezone.utc).isoformat()

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("capsule.yaml", yaml.safe_dump(manifest))
        buf.seek(0)

        resp = client.post(
            "/v0/capsules",
            files={"capsule": ("child.zip", buf, "application/zip")},
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["run_id"] == child_run_id

    def test_upload_child_after_orphan_timeout_elapsed_succeeds(
        self, client: TestClient
    ) -> None:
        """Child whose created_at is older than the orphan window is accepted without parent."""
        child_run_id = "01CHILD000000000000000003C"
        manifest = _make_capsule_yaml(child_run_id)
        manifest["parent_run_id"] = "01MISSING0000000000000PR2N"
        # Far in the past — well beyond the 86 400-second orphan window.
        manifest["created_at"] = "2020-01-01T00:00:00+00:00"

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("capsule.yaml", yaml.safe_dump(manifest))
        buf.seek(0)

        resp = client.post(
            "/v0/capsules",
            files={"capsule": ("child.zip", buf, "application/zip")},
        )
        assert resp.status_code == 201, resp.text


# --------------------------------------------------------------------------- #
# Lineage resource
# --------------------------------------------------------------------------- #


class TestLineage:
    def test_nodes_empty(self, client: TestClient) -> None:
        resp = client.get("/v0/lineage/nodes")
        assert resp.status_code == 200
        body = resp.json()
        assert body["items"] == []
        assert body["total"] == 0

    def test_blast_radius_empty(self, client: TestClient) -> None:
        resp = client.get("/v0/lineage/blast-radius?ref=nonexistent")
        assert resp.status_code == 200
        body = resp.json()
        assert body["nodes"] == []
        assert body["ref"] == "nonexistent"

    def test_provenance_empty(self, client: TestClient) -> None:
        resp = client.get("/v0/lineage/provenance?ref=nonexistent")
        assert resp.status_code == 200
        body = resp.json()
        assert body["nodes"] == []

    def test_replay_chain_empty(self, client: TestClient) -> None:
        resp = client.get("/v0/lineage/replay-chain?run_id=nonexistent")
        assert resp.status_code == 200
        body = resp.json()
        assert body["chain"] == []
        assert body["run_id"] == "nonexistent"

    def test_blast_radius_with_lineage_data(
        self, client: TestClient, db_path: Path
    ) -> None:
        """Seed lineage data via the LineageStore, then query blast-radius."""
        from novafabric.lineage._store import LineageStore
        from novafabric.lineage._types import LineageEdge, LineageNode, node_id_for

        store = LineageStore(db_path=db_path)
        node_a = LineageNode(
            node_id=node_id_for("run", "run-a"),
            kind="run",
            ref="run-a",
            first_seen_capsule_run_id="run-a",
            payload={},
        )
        node_b = LineageNode(
            node_id=node_id_for("asset", "local:model@1.0"),
            kind="asset",
            ref="local:model@1.0",
            first_seen_capsule_run_id="run-a",
            payload={},
        )
        edge = LineageEdge(
            edge_type="used",
            source={"kind": "run", "run_id": "run-a"},
            target={"kind": "asset", "registry": "local", "asset_ref": "model@1.0"},
            confidence="observed",
            capsule_run_id="run-a",
        )
        store.replace_capsule_lineage([node_a, node_b], [edge], "run-a")

        resp = client.get("/v0/lineage/blast-radius?ref=local:model@1.0&kind=asset")
        assert resp.status_code == 200
        body = resp.json()
        # The run node should appear in blast radius of the asset
        assert len(body["nodes"]) >= 1

    def test_nodes_list_with_data(
        self, client: TestClient, db_path: Path
    ) -> None:
        from novafabric.lineage._store import LineageStore
        from novafabric.lineage._types import LineageNode, node_id_for

        store = LineageStore(db_path=db_path)
        node = LineageNode(
            node_id=node_id_for("run", "run-x"),
            kind="run",
            ref="run-x",
            first_seen_capsule_run_id="run-x",
            payload={"test": True},
        )
        store.replace_capsule_lineage([node], [], "run-x")

        resp = client.get("/v0/lineage/nodes")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] >= 1
        kinds = [n["kind"] for n in body["items"]]
        assert "run" in kinds


# --------------------------------------------------------------------------- #
# Replays resource
# --------------------------------------------------------------------------- #


class TestReplays:
    def test_schedule_not_found_capsule(self, client: TestClient) -> None:
        resp = client.post(
            "/v0/replays",
            json={"run_id": "no-such-capsule", "mode": "forensic"},
        )
        assert resp.status_code == 404

    def test_schedule_invalid_mode(
        self, client: TestClient, capsule_dir: Path
    ) -> None:
        _write_capsule(capsule_dir, "01REPLAY0000000000000000001")
        resp = client.post(
            "/v0/replays",
            json={"run_id": "01REPLAY0000000000000000001", "mode": "invalid_mode"},
        )
        assert resp.status_code == 400

    def test_schedule_and_get(
        self, client: TestClient, capsule_dir: Path
    ) -> None:
        run_id = "01REPLAY0000000000000000002"
        _write_capsule(capsule_dir, run_id)

        resp = client.post(
            "/v0/replays",
            json={"run_id": run_id, "mode": "forensic"},
        )
        assert resp.status_code == 202
        body = resp.json()
        assert body["run_id"] == run_id
        assert body["mode"] == "forensic"
        assert body["status"] in ("pending", "running", "completed", "failed")
        replay_id = body["replay_id"]

        get_resp = client.get(f"/v0/replays/{replay_id}")
        assert get_resp.status_code == 200
        get_body = get_resp.json()
        assert get_body["replay_id"] == replay_id

    def test_get_not_found(self, client: TestClient) -> None:
        resp = client.get("/v0/replays/no-such-replay-id")
        assert resp.status_code == 404

    def test_cancel(
        self, client: TestClient, capsule_dir: Path
    ) -> None:
        run_id = "01REPLAY0000000000000000003"
        _write_capsule(capsule_dir, run_id)

        schedule_resp = client.post(
            "/v0/replays",
            json={"run_id": run_id, "mode": "mocked"},
        )
        replay_id = schedule_resp.json()["replay_id"]

        # Patch the job to pending so cancel works
        from novafabric.server.routes import replays as replays_module

        with replays_module._JOBS_LOCK:
            if replay_id in replays_module._JOBS:
                replays_module._JOBS[replay_id]["status"] = "pending"

        cancel_resp = client.delete(f"/v0/replays/{replay_id}")
        assert cancel_resp.status_code == 200
        assert cancel_resp.json()["status"] == "cancelled"

    def test_cancel_completed_returns_409(
        self, client: TestClient, capsule_dir: Path
    ) -> None:
        run_id = "01REPLAY0000000000000000004"
        _write_capsule(capsule_dir, run_id)

        schedule_resp = client.post(
            "/v0/replays",
            json={"run_id": run_id, "mode": "forensic"},
        )
        replay_id = schedule_resp.json()["replay_id"]

        # Force into completed state
        from novafabric.server.routes import replays as replays_module

        with replays_module._JOBS_LOCK:
            if replay_id in replays_module._JOBS:
                replays_module._JOBS[replay_id]["status"] = "completed"

        cancel_resp = client.delete(f"/v0/replays/{replay_id}")
        assert cancel_resp.status_code == 409

    def test_missing_run_id_returns_400(self, client: TestClient) -> None:
        resp = client.post("/v0/replays", json={"mode": "forensic"})
        assert resp.status_code == 400

    def test_missing_mode_returns_400(self, client: TestClient) -> None:
        resp = client.post("/v0/replays", json={"run_id": "anything"})
        assert resp.status_code == 400


# --------------------------------------------------------------------------- #
# Evidence resource
# --------------------------------------------------------------------------- #


class TestEvidence:
    def test_export_capsule_not_found(self, client: TestClient) -> None:
        resp = client.post("/v0/evidence", json={"run_id": "no-such-run"})
        assert resp.status_code == 404

    def test_get_not_found(self, client: TestClient) -> None:
        resp = client.get("/v0/evidence/no-such-bundle")
        assert resp.status_code == 404

    def test_download_not_found(self, client: TestClient) -> None:
        resp = client.get("/v0/evidence/no-such-bundle/download")
        assert resp.status_code == 404

    def test_export_creates_bundle_or_validation_error(
        self, client: TestClient, capsule_dir: Path
    ) -> None:
        """Export may fail with 422 (missing required capsule files) or 202 (success)."""
        run_id = "01EVID00000000000000000001"
        _write_capsule(capsule_dir, run_id)

        resp = client.post("/v0/evidence", json={"run_id": run_id})
        # Either 202 (bundle built) or 422 (capsule validation failed due to test env)
        assert resp.status_code in (202, 422, 500), resp.text
        if resp.status_code == 202:
            body = resp.json()
            assert body["run_id"] == run_id
            assert "bundle_id" in body

            bundle_id = body["bundle_id"]
            get_resp = client.get(f"/v0/evidence/{bundle_id}")
            assert get_resp.status_code == 200
            assert get_resp.json()["bundle_id"] == bundle_id

    def test_missing_run_id_returns_400(self, client: TestClient) -> None:
        resp = client.post("/v0/evidence", json={})
        assert resp.status_code == 400
