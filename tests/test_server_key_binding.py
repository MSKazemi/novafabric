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
"""ADR-0294 D2: request-time enforcement of the API-key workspace binding.

- off by default: a bound key may declare any workspace, bind to a ghost slug;
- on: unknown binding ⇒ 403 workspace_binding_invalid; declared workspace
  (header or ``workspace`` query) ≠ binding ⇒ 403 workspace_binding_mismatch;
- unbound keys and non-key credentials unaffected;
- every refusal audited, bounded per (subject, reason, requested) window.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from novafabric.server import api_keys, key_binding, workspace_store  # noqa: E402
from novafabric.server.app import create_app  # noqa: E402
from novafabric.server.config import ApiKeysConfig, ServerConfig  # noqa: E402

HEADER = key_binding.WORKSPACE_HEADER


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db = tmp_path / "binding-test.db"
    monkeypatch.setenv("NOVAFABRIC_DB_PATH", str(db))
    monkeypatch.setenv("NOVAFABRIC_CAPSULE_DIR", str(tmp_path / "capsules"))
    (tmp_path / "capsules").mkdir()
    workspace_store.ensure_default(db_path=db)
    org = workspace_store.create_org("acme", "Acme", "t", db_path=db)
    workspace_store.create_workspace(org["id"], "ml", "ML", "t", db_path=db)
    return db


@pytest.fixture
def audit_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "audit.jsonl"
    monkeypatch.setenv("NOVAFABRIC_DASHBOARD_AUDIT_FILE", str(path))
    key_binding._audit_window.clear()  # noqa: SLF001 — isolate the window per test
    return path


def _key(db_path: Path, workspace: str | None, owner: str = "svc") -> str:
    key, _ = api_keys.create_key(
        owner, ["reader", "writer"], actor="t", workspace=workspace, db_path=db_path
    )
    return key


def _client(db_path: Path, *, enforce: bool) -> TestClient:
    cfg = ServerConfig(
        db_path=str(db_path),
        insecure_no_auth=True,
        api_keys=ApiKeysConfig(enforce_workspace_binding=enforce),
    )
    return TestClient(create_app(cfg), raise_server_exceptions=False)


def _get(client: TestClient, key: str, path: str = "/v0/lineage/nodes", **headers: str):
    return client.get(path, headers={"Authorization": f"Bearer {key}", **headers})


def _audit_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return [r for r in rows if r.get("action") == key_binding.AUDIT_ACTION]


class TestDefaultOff:
    def test_mismatch_and_ghost_binding_allowed(self, db_path: Path, audit_file: Path) -> None:
        client = _client(db_path, enforce=False)
        assert _get(client, _key(db_path, "ml"), **{HEADER: "other"}).status_code == 200
        assert _get(client, _key(db_path, "ghost")).status_code == 200
        assert _audit_rows(audit_file) == []

    def test_config_default_and_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert ServerConfig().api_keys.enforce_workspace_binding is False
        monkeypatch.setenv("NOVAFABRIC_SERVER_API_KEYS_ENFORCE_WORKSPACE_BINDING", "true")
        assert ServerConfig().api_keys.enforce_workspace_binding is True


class TestEnforced:
    def test_matching_binding_passes(self, db_path: Path, audit_file: Path) -> None:
        client = _client(db_path, enforce=True)
        key = _key(db_path, "ml")
        assert _get(client, key).status_code == 200
        assert _get(client, key, **{HEADER: "ml"}).status_code == 200
        assert _audit_rows(audit_file) == []

    def test_header_mismatch_refused_and_audited(self, db_path: Path, audit_file: Path) -> None:
        client = _client(db_path, enforce=True)
        resp = _get(client, _key(db_path, "ml"), **{HEADER: "default"})
        assert resp.status_code == 403
        err = resp.json()["error"]
        assert err["code"] == "workspace_binding_mismatch"
        assert err["details"] == {"binding": "ml", "requested": "default", "source": "header"}
        rows = _audit_rows(audit_file)
        assert len(rows) == 1
        assert rows[0]["result"] == "refused"
        assert rows[0]["args"]["reason"] == "workspace_binding_mismatch"
        assert rows[0]["args"]["subject"] == "svc"
        assert "nvfk_" not in audit_file.read_text()  # never the key itself

    def test_query_mismatch_refused(self, db_path: Path, audit_file: Path) -> None:
        client = _client(db_path, enforce=True)
        resp = _get(client, _key(db_path, "ml"), path="/v0/usage?workspace=default")
        assert resp.status_code == 403
        assert resp.json()["error"]["details"]["source"] == "query"

    def test_query_match_passes_binding_check(self, db_path: Path, audit_file: Path) -> None:
        client = _client(db_path, enforce=True)
        resp = _get(client, _key(db_path, "ml"), path="/v0/usage?workspace=ml")
        assert resp.status_code != 403

    def test_unknown_binding_refused(self, db_path: Path, audit_file: Path) -> None:
        client = _client(db_path, enforce=True)
        resp = _get(client, _key(db_path, "ghost"))
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "workspace_binding_invalid"
        rows = _audit_rows(audit_file)
        assert rows and rows[0]["args"]["binding"] == "ghost"

    def test_upload_with_ghost_binding_refused(self, db_path: Path, audit_file: Path) -> None:
        import io
        import zipfile

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("capsule.yaml", "run_id: g1\nstatus: completed\n")
        client = _client(db_path, enforce=True)
        resp = client.post(
            "/v0/capsules",
            files={"capsule": ("g1.zip", buf.getvalue(), "application/zip")},
            headers={"Authorization": f"Bearer {_key(db_path, 'ghost')}"},
        )
        assert resp.status_code == 403

    def test_unbound_key_unaffected(self, db_path: Path, audit_file: Path) -> None:
        client = _client(db_path, enforce=True)
        assert _get(client, _key(db_path, None), **{HEADER: "anything"}).status_code == 200

    def test_non_key_credential_unaffected(self, db_path: Path, audit_file: Path) -> None:
        client = _client(db_path, enforce=True)
        assert client.get("/v0/lineage/nodes", headers={HEADER: "anything"}).status_code == 200

    def test_audit_bounded_per_window(self, db_path: Path, audit_file: Path) -> None:
        client = _client(db_path, enforce=True)
        key = _key(db_path, "ml")
        for _ in range(5):
            assert _get(client, key, **{HEADER: "default"}).status_code == 403
        assert _get(client, key, **{HEADER: "other"}).status_code == 403
        rows = _audit_rows(audit_file)
        assert sorted(r["args"]["requested"] for r in rows) == ["default", "other"]

    def test_store_failure_fails_closed(
        self, db_path: Path, audit_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(_db: object) -> object:
            raise RuntimeError("store down")

        client = _client(db_path, enforce=True)
        key = _key(db_path, "ml")
        monkeypatch.setattr(workspace_store, "_get_conn", boom)
        assert _get(client, key).status_code == 403


class TestAuditWindow:
    def test_lru_bound_and_expiry(self) -> None:
        now = [0.0]
        window = key_binding._AuditWindow(window_seconds=10, max_keys=2, clock=lambda: now[0])  # noqa: SLF001
        assert window.should_emit(("a",))
        assert not window.should_emit(("a",))
        assert window.should_emit(("b",))
        assert window.should_emit(("c",))  # evicts "a"
        assert window.should_emit(("a",))
        now[0] = 11.0
        assert window.should_emit(("b",)) or window.should_emit(("c",))
