"""Every server call into the registry stores passes the configured ``db_path``.

Review server-05: ``server.db_path`` / ``NOVAFABRIC_SERVER_DB_PATH`` was honoured
by some stores (API keys, usage, webhooks, the startup checks) and ignored by
others (orgs, workspaces, roles, service accounts, SCIM, the role fallback), so
one server wrote two registries. Orgs created over the API landed in
``$NOVAFABRIC_HOME/registry.db`` while startup counted orgs in the configured
database, so the multi-org shared-store guard never fired; API keys were created
in one database and verified against the other.

The structural guard reads ``src/novafabric/server`` with :mod:`ast`: any call
to a ``workspace_store`` / ``rbac_store`` / ``scim_store`` function outside the
store modules must pass ``db_path=``. The behavioural tests drive the real app
with ``db_path`` set.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from novafabric.server.app import create_app  # noqa: E402
from novafabric.server.config import ServerConfig  # noqa: E402

SERVER = Path(__file__).resolve().parents[1] / "src" / "novafabric" / "server"
_STORES = {"workspace_store", "rbac_store", "scim_store"}
TOKEN = "test-local-token-dbpath"
ADMIN = {"Authorization": f"Bearer {TOKEN}"}


def _store_calls_without_db_path() -> list[str]:
    missing: list[str] = []
    for path in sorted(SERVER.rglob("*.py")):
        if path.stem in _STORES:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            owner = node.func.value
            if not (isinstance(owner, ast.Name) and owner.id in _STORES):
                continue
            if node.func.attr[0].isupper() or node.func.attr.endswith("Error"):
                continue  # an exception class or constant, not a store call
            if not any(k.arg == "db_path" for k in node.keywords):
                rel = path.relative_to(SERVER).as_posix()
                missing.append(f"{rel}:{node.lineno} {owner.id}.{node.func.attr}()")
    return missing


def test_every_store_call_passes_db_path() -> None:
    missing = _store_calls_without_db_path()
    assert not missing, (
        "these calls use the default registry even when server.db_path is set; pass "
        "db_path= (from Depends(get_db_path), or request.app.state.config):\n  "
        + "\n  ".join(missing)
    )


def _client(db: Path) -> TestClient:
    cfg = ServerConfig(db_path=str(db), local_token=TOKEN)
    return TestClient(create_app(cfg), raise_server_exceptions=False)


def test_orgs_created_over_the_api_land_in_the_configured_registry(tmp_path: Path) -> None:
    from novafabric.server import workspace_store
    from novafabric.server.config import UnpartitionedStoreError

    db = tmp_path / "configured.db"
    with _client(db) as client:
        for slug in ("acme", "globex"):
            resp = client.post("/v0/orgs", json={"slug": slug, "name": slug}, headers=ADMIN)
            assert resp.status_code == 201, resp.text
        listed = client.get("/v0/orgs", headers=ADMIN).json()["orgs"]
    assert {"acme", "globex"} <= {o["slug"] for o in listed}
    assert {"acme", "globex"} <= {o["slug"] for o in workspace_store.list_orgs(db_path=db)}
    assert not {"acme", "globex"} & {o["slug"] for o in workspace_store.list_orgs()}
    with pytest.raises(UnpartitionedStoreError), _client(db):
        pass  # the shared-store guard counts the orgs where they were written


def test_api_key_created_with_db_path_authenticates(tmp_path: Path) -> None:
    with _client(tmp_path / "configured.db") as client:
        resp = client.post("/v0/api-keys", json={"owner": "ci", "roles": ["reader"]}, headers=ADMIN)
        assert resp.status_code == 201, resp.text
        key = {"Authorization": f"Bearer {resp.json()['key']}"}
        assert client.get("/v0/assets", headers=key).status_code == 200
