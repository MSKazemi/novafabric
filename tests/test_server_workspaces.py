"""Tests for the workspace/org model + service accounts (ADR-0178, first slice).

Covers:
- default org/workspace bootstrap (idempotent, and via app lifespan)
- org/workspace CRUD + slug uniqueness + delete-only-when-empty
- membership grant/revoke (idempotent grant, six-role vocabulary closed)
- effective_roles union: global-only (legacy), org-scoped, workspace-scoped,
  orthogonal-role union, and the global-only short-circuit regression
- service accounts: create mints an offline ed25519 token (subject svc:<name>,
  returned once), token authenticates with roles resolved from memberships or
  global assignments, disable revokes the token (401)
- admin gating (non-admin 403, unauthenticated 401)
- works with OIDC off via the ADR-0184 local token and via insecure_no_auth

Registry-tier scoping only — no tenant_id/RLS surface is involved (spec I1/I2).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from novafabric.server import rbac_store, workspace_store  # noqa: E402
from novafabric.server.app import create_app  # noqa: E402
from novafabric.server.config import ServerConfig  # noqa: E402
from novafabric.server.offline_tokens import generate_keypair, issue_token  # noqa: E402

TEST_TOKEN = "test-local-token-adr0178"


def _client(tmp_path: Path, **cfg_kwargs: object) -> TestClient:
    cfg = ServerConfig(db_path=str(tmp_path / "test.db"), **cfg_kwargs)  # type: ignore[arg-type]
    return TestClient(create_app(cfg), raise_server_exceptions=False)


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


ADMIN = _bearer(TEST_TOKEN)


def _key_path(tmp_path: Path) -> Path:
    return tmp_path / "keys" / "offline-key.pem"


def _svc_client(tmp_path: Path) -> TestClient:
    """Client with local-token admin auth AND the offline signing key configured."""
    return _client(
        tmp_path,
        local_token=TEST_TOKEN,
        offline_key_path=str(_key_path(tmp_path)),
    )


def _create_org(client: TestClient, slug: str = "acme") -> dict[str, Any]:
    resp = client.post("/v0/orgs", json={"slug": slug, "name": slug.title()}, headers=ADMIN)
    assert resp.status_code == 201, resp.text
    return resp.json()["org"]


def _create_workspace(
    client: TestClient, org_id: str, slug: str = "ml-team"
) -> dict[str, Any]:
    resp = client.post(
        "/v0/workspaces",
        json={"org_id": org_id, "slug": slug, "name": slug.title()},
        headers=ADMIN,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["workspace"]


# ---------------------------------------------------------------------------
# Default bootstrap
# ---------------------------------------------------------------------------


class TestDefaultBootstrap:
    def test_ensure_default_idempotent(self) -> None:
        first = workspace_store.ensure_default()
        second = workspace_store.ensure_default()
        assert first == second
        orgs = workspace_store.list_orgs()
        assert [o["slug"] for o in orgs] == ["default"]
        workspaces = workspace_store.list_workspaces()
        assert [w["slug"] for w in workspaces] == ["default"]
        assert workspaces[0]["org_id"] == orgs[0]["id"]

    def test_lifespan_bootstraps_default(self) -> None:
        # db_path=None → lifespan and routes resolve the same hermetic registry DB
        cfg = ServerConfig(local_token=TEST_TOKEN)
        with TestClient(create_app(cfg), raise_server_exceptions=False) as client:
            resp = client.get("/v0/orgs", headers=ADMIN)
            assert resp.status_code == 200
            assert "default" in [o["slug"] for o in resp.json()["orgs"]]


# ---------------------------------------------------------------------------
# Org CRUD
# ---------------------------------------------------------------------------


class TestOrgCrud:
    def test_create_and_get(self, tmp_path: Path) -> None:
        client = _client(tmp_path, local_token=TEST_TOKEN)
        org = _create_org(client)
        assert org["slug"] == "acme"
        assert org["created_by"]
        resp = client.get(f"/v0/orgs/{org['id']}", headers=ADMIN)
        assert resp.status_code == 200
        assert resp.json()["org"]["id"] == org["id"]

    def test_duplicate_slug_conflict(self, tmp_path: Path) -> None:
        client = _client(tmp_path, local_token=TEST_TOKEN)
        _create_org(client)
        resp = client.post("/v0/orgs", json={"slug": "acme", "name": "Dup"}, headers=ADMIN)
        assert resp.status_code == 409

    def test_invalid_slug_rejected(self, tmp_path: Path) -> None:
        client = _client(tmp_path, local_token=TEST_TOKEN)
        resp = client.post("/v0/orgs", json={"slug": "Not Valid!", "name": "x"}, headers=ADMIN)
        assert resp.status_code == 422

    def test_list(self, tmp_path: Path) -> None:
        client = _client(tmp_path, local_token=TEST_TOKEN)
        _create_org(client, "a-org")
        _create_org(client, "b-org")
        resp = client.get("/v0/orgs", headers=ADMIN)
        assert [o["slug"] for o in resp.json()["orgs"]] == ["a-org", "b-org"]

    def test_get_missing_404(self, tmp_path: Path) -> None:
        client = _client(tmp_path, local_token=TEST_TOKEN)
        assert client.get("/v0/orgs/nope", headers=ADMIN).status_code == 404

    def test_delete_empty_org(self, tmp_path: Path) -> None:
        client = _client(tmp_path, local_token=TEST_TOKEN)
        org = _create_org(client)
        assert client.delete(f"/v0/orgs/{org['id']}", headers=ADMIN).status_code == 200
        assert client.delete(f"/v0/orgs/{org['id']}", headers=ADMIN).status_code == 404

    def test_delete_non_empty_org_conflict(self, tmp_path: Path) -> None:
        client = _client(tmp_path, local_token=TEST_TOKEN)
        org = _create_org(client)
        _create_workspace(client, org["id"])
        resp = client.delete(f"/v0/orgs/{org['id']}", headers=ADMIN)
        assert resp.status_code == 409


# ---------------------------------------------------------------------------
# Workspace CRUD
# ---------------------------------------------------------------------------


class TestWorkspaceCrud:
    def test_create_and_get(self, tmp_path: Path) -> None:
        client = _client(tmp_path, local_token=TEST_TOKEN)
        org = _create_org(client)
        ws = _create_workspace(client, org["id"])
        resp = client.get(f"/v0/workspaces/{ws['id']}", headers=ADMIN)
        assert resp.status_code == 200
        body = resp.json()
        assert body["workspace"]["org_id"] == org["id"]
        assert body["memberships"] == []

    def test_slug_unique_within_org_only(self, tmp_path: Path) -> None:
        client = _client(tmp_path, local_token=TEST_TOKEN)
        org_a = _create_org(client, "org-a")
        org_b = _create_org(client, "org-b")
        _create_workspace(client, org_a["id"], "team")
        dup = client.post(
            "/v0/workspaces",
            json={"org_id": org_a["id"], "slug": "team", "name": "Team"},
            headers=ADMIN,
        )
        assert dup.status_code == 409
        # Same slug in a different org is fine
        _create_workspace(client, org_b["id"], "team")

    def test_create_in_unknown_org_404(self, tmp_path: Path) -> None:
        client = _client(tmp_path, local_token=TEST_TOKEN)
        resp = client.post(
            "/v0/workspaces",
            json={"org_id": "nope", "slug": "team", "name": "Team"},
            headers=ADMIN,
        )
        assert resp.status_code == 404

    def test_list_filter_by_org(self, tmp_path: Path) -> None:
        client = _client(tmp_path, local_token=TEST_TOKEN)
        org_a = _create_org(client, "org-a")
        org_b = _create_org(client, "org-b")
        _create_workspace(client, org_a["id"], "a-ws")
        _create_workspace(client, org_b["id"], "b-ws")
        resp = client.get("/v0/workspaces", params={"org_id": org_a["id"]}, headers=ADMIN)
        assert [w["slug"] for w in resp.json()["workspaces"]] == ["a-ws"]
        resp = client.get("/v0/workspaces", headers=ADMIN)
        assert len(resp.json()["workspaces"]) == 2

    def test_delete_only_when_empty(self, tmp_path: Path) -> None:
        client = _client(tmp_path, local_token=TEST_TOKEN)
        org = _create_org(client)
        ws = _create_workspace(client, org["id"])
        client.post(
            f"/v0/workspaces/{ws['id']}/memberships",
            json={"principal": "alice@example.com", "role": "reader"},
            headers=ADMIN,
        )
        assert client.delete(f"/v0/workspaces/{ws['id']}", headers=ADMIN).status_code == 409
        client.delete(
            f"/v0/workspaces/{ws['id']}/memberships/alice@example.com/reader",
            headers=ADMIN,
        )
        assert client.delete(f"/v0/workspaces/{ws['id']}", headers=ADMIN).status_code == 200
        assert client.delete(f"/v0/workspaces/{ws['id']}", headers=ADMIN).status_code == 404


# ---------------------------------------------------------------------------
# Memberships
# ---------------------------------------------------------------------------


class TestMemberships:
    def test_grant_and_revoke(self, tmp_path: Path) -> None:
        client = _client(tmp_path, local_token=TEST_TOKEN)
        org = _create_org(client)
        ws = _create_workspace(client, org["id"])
        resp = client.post(
            f"/v0/workspaces/{ws['id']}/memberships",
            json={"principal": "alice@example.com", "role": "writer"},
            headers=ADMIN,
        )
        assert resp.status_code == 201
        assert resp.json()["created"] is True
        # Idempotent re-grant
        resp = client.post(
            f"/v0/workspaces/{ws['id']}/memberships",
            json={"principal": "alice@example.com", "role": "writer"},
            headers=ADMIN,
        )
        assert resp.status_code == 201
        assert resp.json()["created"] is False
        # Visible in the workspace detail
        detail = client.get(f"/v0/workspaces/{ws['id']}", headers=ADMIN).json()
        assert len(detail["memberships"]) == 1
        # Revoke
        resp = client.delete(
            f"/v0/workspaces/{ws['id']}/memberships/alice@example.com/writer",
            headers=ADMIN,
        )
        assert resp.status_code == 200
        resp = client.delete(
            f"/v0/workspaces/{ws['id']}/memberships/alice@example.com/writer",
            headers=ADMIN,
        )
        assert resp.status_code == 404

    def test_role_vocabulary_closed(self, tmp_path: Path) -> None:
        client = _client(tmp_path, local_token=TEST_TOKEN)
        org = _create_org(client)
        ws = _create_workspace(client, org["id"])
        resp = client.post(
            f"/v0/workspaces/{ws['id']}/memberships",
            json={"principal": "alice@example.com", "role": "superuser"},
            headers=ADMIN,
        )
        assert resp.status_code == 422

    def test_grant_on_unknown_workspace_404(self, tmp_path: Path) -> None:
        client = _client(tmp_path, local_token=TEST_TOKEN)
        resp = client.post(
            "/v0/workspaces/nope/memberships",
            json={"principal": "alice@example.com", "role": "reader"},
            headers=ADMIN,
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Effective-role resolution (spec I3)
# ---------------------------------------------------------------------------


class TestEffectiveRoles:
    def test_global_only_legacy(self) -> None:
        rbac_store.assign_role("alice", "writer", "test")
        assert workspace_store.effective_roles("alice") == {"writer"}

    def test_org_scoped(self) -> None:
        org = workspace_store.create_org("acme", "Acme", "test")
        ws = workspace_store.create_workspace(org["id"], "team", "Team", "test")
        other_org = workspace_store.create_org("other", "Other", "test")
        other_ws = workspace_store.create_workspace(other_org["id"], "team", "Team", "test")
        workspace_store.add_membership("bob", "org", org["id"], "writer", "test")
        assert workspace_store.effective_roles("bob") == {"writer"}
        # Org binding applies to workspaces of the containing org only
        assert workspace_store.effective_roles("bob", ws["id"]) == {"writer"}
        assert workspace_store.effective_roles("bob", other_ws["id"]) == set()

    def test_workspace_scoped(self) -> None:
        org = workspace_store.create_org("acme", "Acme", "test")
        ws_a = workspace_store.create_workspace(org["id"], "a", "A", "test")
        ws_b = workspace_store.create_workspace(org["id"], "b", "B", "test")
        workspace_store.add_membership("carol", "workspace", ws_a["id"], "reader", "test")
        assert workspace_store.effective_roles("carol", ws_a["id"]) == {"reader"}
        assert workspace_store.effective_roles("carol", ws_b["id"]) == set()
        assert workspace_store.effective_roles("carol") == {"reader"}

    def test_union_across_scopes_and_orthogonal_roles(self) -> None:
        org = workspace_store.create_org("acme", "Acme", "test")
        ws = workspace_store.create_workspace(org["id"], "team", "Team", "test")
        rbac_store.assign_role("dave", "auditor", "test")
        workspace_store.add_membership("dave", "org", org["id"], "promoter", "test")
        workspace_store.add_membership("dave", "workspace", ws["id"], "writer", "test")
        assert workspace_store.effective_roles("dave", ws["id"]) == {
            "auditor",
            "promoter",
            "writer",
        }

    def test_invalid_role_rejected_at_store(self) -> None:
        org = workspace_store.create_org("acme", "Acme", "test")
        with pytest.raises(workspace_store.InvalidRoleError):
            workspace_store.add_membership("eve", "org", org["id"], "root", "test")

    def test_membership_on_unknown_scope_rejected(self) -> None:
        with pytest.raises(workspace_store.UnknownScopeError):
            workspace_store.add_membership("eve", "org", "nope", "reader", "test")


# ---------------------------------------------------------------------------
# Global-only short-circuit regression (require_role wiring)
# ---------------------------------------------------------------------------


class TestGlobalOnlyShortCircuit:
    def test_satisfied_token_never_consults_workspace_store(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A token whose own roles satisfy the check must not touch the store."""
        calls: list[str] = []

        def _record(*args: object, **kwargs: object) -> bool:
            calls.append("consulted")
            return False

        monkeypatch.setattr(workspace_store, "feature_in_use", _record)
        monkeypatch.setattr(workspace_store, "effective_roles", _record)
        client = _client(tmp_path, local_token=TEST_TOKEN)
        assert client.get("/v0/assets", headers=ADMIN).status_code == 200
        assert client.post("/v0/admin/flush-jwks", headers=ADMIN).status_code == 200
        assert calls == []

    def test_forbidden_identical_when_feature_unused(self, tmp_path: Path) -> None:
        """No memberships/service accounts → 403 path is byte-identical."""
        key = _key_path(tmp_path)
        generate_keypair(key)
        reader_token = issue_token("human", ["reader"], 1, key)
        client = _svc_client(tmp_path)
        resp = client.get("/v0/orgs", headers=_bearer(reader_token))
        assert resp.status_code == 403
        assert (
            resp.json()["error"]["message"]
            == "Role 'admin' or higher required; token has ['reader']"
        )

    def test_membership_grants_access_when_token_roles_insufficient(
        self, tmp_path: Path
    ) -> None:
        key = _key_path(tmp_path)
        generate_keypair(key)
        no_role_token = issue_token("frank", [], 1, key)
        client = _svc_client(tmp_path)
        org = _create_org(client)
        ws = _create_workspace(client, org["id"])
        # Without any binding: 403
        assert client.get("/v0/assets", headers=_bearer(no_role_token)).status_code == 403
        # Workspace-scoped reader binding makes the same token pass
        client.post(
            f"/v0/workspaces/{ws['id']}/memberships",
            json={"principal": "frank", "role": "reader"},
            headers=ADMIN,
        )
        assert client.get("/v0/assets", headers=_bearer(no_role_token)).status_code == 200


# ---------------------------------------------------------------------------
# Service accounts
# ---------------------------------------------------------------------------


class TestServiceAccounts:
    def _create(self, client: TestClient, name: str = "ci-ingest") -> dict[str, Any]:
        resp = client.post("/v0/service-accounts", json={"name": name}, headers=ADMIN)
        assert resp.status_code == 201, resp.text
        return resp.json()

    def test_create_returns_token_once(self, tmp_path: Path) -> None:
        client = _svc_client(tmp_path)
        body = self._create(client)
        assert body["subject"] == "svc:ci-ingest"
        assert body["token"].count(".") == 2
        assert body["token_id"]
        assert body["service_account"]["disabled"] == 0
        # List never exposes the token itself
        listed = client.get("/v0/service-accounts", headers=ADMIN).json()
        assert [a["name"] for a in listed["service_accounts"]] == ["ci-ingest"]
        assert "token" not in listed["service_accounts"][0]

    def test_duplicate_name_conflict(self, tmp_path: Path) -> None:
        client = _svc_client(tmp_path)
        self._create(client)
        resp = client.post(
            "/v0/service-accounts", json={"name": "ci-ingest"}, headers=ADMIN
        )
        assert resp.status_code == 409

    def test_create_without_offline_key_refused(self, tmp_path: Path) -> None:
        client = _client(tmp_path, local_token=TEST_TOKEN)  # no offline_key_path
        resp = client.post("/v0/service-accounts", json={"name": "ci"}, headers=ADMIN)
        assert resp.status_code == 400
        assert "NOVAFABRIC_OFFLINE_KEY_PATH" in resp.json()["error"]["message"]

    def test_token_authenticates_with_membership_role(self, tmp_path: Path) -> None:
        client = _svc_client(tmp_path)
        token = self._create(client)["token"]
        # Bare service-account token: authenticated (not 401) but no roles → 403
        assert client.get("/v0/assets", headers=_bearer(token)).status_code == 403
        org = _create_org(client)
        ws = _create_workspace(client, org["id"])
        client.post(
            f"/v0/workspaces/{ws['id']}/memberships",
            json={"principal": "svc:ci-ingest", "role": "reader"},
            headers=ADMIN,
        )
        assert client.get("/v0/assets", headers=_bearer(token)).status_code == 200

    def test_token_authenticates_with_global_assignment(self, tmp_path: Path) -> None:
        client = _svc_client(tmp_path)
        token = self._create(client, name="ci-global")["token"]
        resp = client.post(
            "/v0/admin/roles",
            json={"subject": "svc:ci-global", "role": "reader"},
            headers=ADMIN,
        )
        assert resp.status_code == 201
        assert client.get("/v0/assets", headers=_bearer(token)).status_code == 200

    def test_disable_revokes_token(self, tmp_path: Path) -> None:
        client = _svc_client(tmp_path)
        body = self._create(client)
        account_id = body["service_account"]["id"]
        token = body["token"]
        rbac_store.assign_role("svc:ci-ingest", "reader", "test")
        assert client.get("/v0/assets", headers=_bearer(token)).status_code == 200
        resp = client.post(f"/v0/service-accounts/{account_id}/disable", headers=ADMIN)
        assert resp.status_code == 200
        assert resp.json()["token_revoked"] is True
        assert client.get("/v0/assets", headers=_bearer(token)).status_code == 401

    def test_disable_unknown_404(self, tmp_path: Path) -> None:
        client = _svc_client(tmp_path)
        resp = client.post("/v0/service-accounts/nope/disable", headers=ADMIN)
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Auth gating (401 / 403) and insecure_no_auth mode
# ---------------------------------------------------------------------------


class TestAuthGating:
    def test_unauthenticated_401(self, tmp_path: Path) -> None:
        client = _client(tmp_path, local_token=TEST_TOKEN)
        for path in ("/v0/orgs", "/v0/workspaces", "/v0/service-accounts"):
            assert client.get(path).status_code == 401

    def test_non_admin_403(self, tmp_path: Path) -> None:
        key = _key_path(tmp_path)
        generate_keypair(key)
        reader_token = issue_token("reader-human", ["reader"], 1, key)
        client = _svc_client(tmp_path)
        headers = _bearer(reader_token)
        assert client.post(
            "/v0/orgs", json={"slug": "x", "name": "X"}, headers=headers
        ).status_code == 403
        assert client.get("/v0/orgs", headers=headers).status_code == 403
        assert client.post(
            "/v0/service-accounts", json={"name": "x"}, headers=headers
        ).status_code == 403

    def test_insecure_no_auth_mode_works(self, tmp_path: Path) -> None:
        client = _client(tmp_path, insecure_no_auth=True)
        resp = client.post("/v0/orgs", json={"slug": "anon-org", "name": "Anon"})
        assert resp.status_code == 201
        assert client.get("/v0/orgs").status_code == 200

    def test_insecure_mode_still_anonymous_admin_with_garbage_bearer(
        self, tmp_path: Path
    ) -> None:
        """insecure_no_auth keeps its pre-0178 behavior even for JWT-shaped junk."""
        client = _client(
            tmp_path,
            insecure_no_auth=True,
            offline_key_path=str(_key_path(tmp_path)),
        )
        generate_keypair(_key_path(tmp_path))
        resp = client.get("/v0/orgs", headers=_bearer("a.b.c"))
        assert resp.status_code == 200
