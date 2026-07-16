"""Tests for SCIM 2.0 provisioning (ADR-0139, first slice: discovery + Users).

Covers: disabled-by-default 404, provisioning-token auth, Users CRUD,
filter/pagination subset, PII minimization, deactivation revoking role
assignments, the last-admin lockout refusal, hard DELETE, the append-only
provisioning audit log, and the provisioning token's inability to reach /v0.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from novafabric.server import rbac_store, scim_store  # noqa: E402
from novafabric.server.app import create_app  # noqa: E402
from novafabric.server.config import (  # noqa: E402
    OidcConfig,
    ScimConfig,
    ServerConfig,
)

SCIM_TOKEN = "test-provisioning-token"
USER_URN = "urn:ietf:params:scim:schemas:core:2.0:User"
PATCH_OP_URN = "urn:ietf:params:scim:api:messages:2.0:PatchOp"
LIST_RESPONSE_URN = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
ERROR_URN = "urn:ietf:params:scim:api:messages:2.0:Error"


def _make_client(tmp_path: Path, *, enabled: bool = True, token: str | None = SCIM_TOKEN) -> TestClient:
    cfg = ServerConfig(db_path=str(tmp_path / "scim.db"), scim=ScimConfig(enabled=enabled))
    cfg.scim_token = token
    return TestClient(create_app(cfg), raise_server_exceptions=False)


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    return _make_client(tmp_path)


def _hdrs(token: str = SCIM_TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _create_user(client: TestClient, user_name: str = "alice@example.com", **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schemas": [USER_URN],
        "userName": user_name,
        "active": True,
        **extra,
    }
    resp = client.post("/scim/v2/Users", json=payload, headers=_hdrs())
    assert resp.status_code == 201, resp.json()
    return resp.json()


def _deactivate_body() -> dict[str, Any]:
    return {
        "schemas": [PATCH_OP_URN],
        "Operations": [{"op": "replace", "path": "active", "value": False}],
    }


# ---------------------------------------------------------------------------
# Disabled by default / auth gating
# ---------------------------------------------------------------------------


class TestScimGating:
    def test_disabled_by_default_returns_404(self, tmp_path: Path) -> None:
        cfg = ServerConfig(db_path=str(tmp_path / "scim.db"))
        assert cfg.scim.enabled is False
        client = TestClient(create_app(cfg), raise_server_exceptions=False)
        resp = client.get("/scim/v2/Users", headers=_hdrs())
        assert resp.status_code == 404
        # Behaves as if the feature does not exist: no SCIM error envelope.
        assert "schemas" not in resp.json()

    def test_enabled_without_token_returns_404(self, tmp_path: Path) -> None:
        client = _make_client(tmp_path, enabled=True, token=None)
        resp = client.get("/scim/v2/Users", headers=_hdrs())
        assert resp.status_code == 404

    def test_token_without_enabled_flag_returns_404(self, tmp_path: Path) -> None:
        client = _make_client(tmp_path, enabled=False, token=SCIM_TOKEN)
        resp = client.get("/scim/v2/Users", headers=_hdrs())
        assert resp.status_code == 404

    def test_missing_bearer_returns_401(self, client: TestClient) -> None:
        resp = client.get("/scim/v2/Users")
        assert resp.status_code == 401
        assert resp.json()["schemas"] == [ERROR_URN]

    def test_wrong_token_returns_401(self, client: TestClient) -> None:
        resp = client.get("/scim/v2/Users", headers=_hdrs("wrong-token"))
        assert resp.status_code == 401
        body = resp.json()
        assert body["schemas"] == [ERROR_URN]
        assert body["status"] == "401"

    def test_scim_token_grants_nothing_on_v0(self, tmp_path: Path) -> None:
        """The provisioning token is not a JWT: /v0 rejects it under OIDC."""
        cfg = ServerConfig(
            db_path=str(tmp_path / "scim.db"),
            oidc=OidcConfig(issuer_url="https://oidc.example.com", audience="nova"),
            scim=ScimConfig(enabled=True),
        )
        cfg.scim_token = SCIM_TOKEN
        client = TestClient(create_app(cfg), raise_server_exceptions=False)
        resp = client.get("/v0/assets", headers=_hdrs())
        assert resp.status_code == 401

    def test_env_overrides_activate_scim(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NOVAFABRIC_SERVER_SCIM_ENABLED", "true")
        monkeypatch.setenv("NOVAFABRIC_SCIM_TOKEN", SCIM_TOKEN)
        cfg = ServerConfig(db_path=str(tmp_path / "scim.db"))
        assert cfg.scim_active is True
        client = TestClient(create_app(cfg), raise_server_exceptions=False)
        resp = client.get("/scim/v2/ServiceProviderConfig", headers=_hdrs())
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


class TestDiscovery:
    def test_service_provider_config(self, client: TestClient) -> None:
        resp = client.get("/scim/v2/ServiceProviderConfig", headers=_hdrs())
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/scim+json")
        body = resp.json()
        assert body["patch"]["supported"] is True
        assert body["bulk"]["supported"] is False

    def test_resource_types_advertises_user_only(self, client: TestClient) -> None:
        resp = client.get("/scim/v2/ResourceTypes", headers=_hdrs())
        assert resp.status_code == 200
        names = [r["name"] for r in resp.json()["Resources"]]
        assert names == ["User"]  # Groups are P3 (planned), not advertised

    def test_schemas_endpoint(self, client: TestClient) -> None:
        resp = client.get("/scim/v2/Schemas", headers=_hdrs())
        assert resp.status_code == 200
        assert resp.json()["Resources"][0]["id"] == USER_URN


# ---------------------------------------------------------------------------
# Users: create / read / list / filter
# ---------------------------------------------------------------------------


class TestUsersCrud:
    def test_create_returns_scim_resource(self, client: TestClient) -> None:
        body = _create_user(
            client,
            externalId="okta-00u1",
            displayName="Alice Ng",
            emails=[{"value": "alice@example.com", "primary": True}],
        )
        assert body["schemas"] == [USER_URN]
        assert body["userName"] == "alice@example.com"
        assert body["active"] is True
        assert body["externalId"] == "okta-00u1"
        assert body["meta"]["resourceType"] == "User"
        assert body["meta"]["location"] == f"/scim/v2/Users/{body['id']}"

    def test_create_drops_non_authz_pii(self, client: TestClient) -> None:
        body = _create_user(
            client,
            user_name="bob@example.com",
            phoneNumbers=[{"value": "+1-555-0100"}],
            addresses=[{"locality": "Bologna"}],
            department="R&D",
        )
        assert "phoneNumbers" not in body
        assert "addresses" not in body
        assert "department" not in body
        # Also not stored, only the closed subset persists
        stored = scim_store.get_user(body["id"])
        assert stored is not None
        assert stored.user_name == "bob@example.com"

    def test_duplicate_username_conflict_409(self, client: TestClient) -> None:
        _create_user(client)
        resp = client.post(
            "/scim/v2/Users",
            json={"schemas": [USER_URN], "userName": "alice@example.com"},
            headers=_hdrs(),
        )
        assert resp.status_code == 409
        body = resp.json()
        assert body["schemas"] == [ERROR_URN]
        assert body["scimType"] == "uniqueness"

    @pytest.mark.parametrize(
        "payload",
        [
            {"schemas": [USER_URN]},  # missing userName
            {"schemas": [USER_URN], "userName": ""},  # empty userName
            {"schemas": ["urn:wrong"], "userName": "x"},  # bad schemas URN
            {"userName": "x"},  # missing schemas
            {"schemas": [USER_URN], "userName": "x", "active": "yes"},  # non-bool
            {"schemas": [USER_URN], "userName": "x", "emails": "not-a-list"},
            {"schemas": [USER_URN], "userName": "x", "emails": ["not-an-object"]},
            {"schemas": [USER_URN], "userName": "x", "externalId": 42},
            {"schemas": [USER_URN], "userName": "x", "displayName": 42},
            ["not", "an", "object"],  # body is not a SCIM User object
        ],
    )
    def test_malformed_payload_400(self, client: TestClient, payload: Any) -> None:
        resp = client.post("/scim/v2/Users", json=payload, headers=_hdrs())
        assert resp.status_code == 400
        assert resp.json()["schemas"] == [ERROR_URN]

    def test_malformed_json_body_400(self, client: TestClient) -> None:
        resp = client.post(
            "/scim/v2/Users",
            content=b"{not json",
            headers={**_hdrs(), "Content-Type": "application/scim+json"},
        )
        assert resp.status_code == 400

    def test_get_by_id(self, client: TestClient) -> None:
        created = _create_user(client)
        resp = client.get(f"/scim/v2/Users/{created['id']}", headers=_hdrs())
        assert resp.status_code == 200
        assert resp.json()["userName"] == "alice@example.com"

    def test_get_missing_returns_scim_404(self, client: TestClient) -> None:
        resp = client.get("/scim/v2/Users/nonexistent", headers=_hdrs())
        assert resp.status_code == 404
        assert resp.json()["schemas"] == [ERROR_URN]

    def test_list_and_filter_by_username(self, client: TestClient) -> None:
        _create_user(client, user_name="alice@example.com")
        _create_user(client, user_name="bob@example.com")
        resp = client.get("/scim/v2/Users", headers=_hdrs())
        assert resp.status_code == 200
        body = resp.json()
        assert body["schemas"] == [LIST_RESPONSE_URN]
        assert body["totalResults"] == 2

        resp = client.get(
            "/scim/v2/Users",
            params={"filter": 'userName eq "bob@example.com"'},
            headers=_hdrs(),
        )
        body = resp.json()
        assert body["totalResults"] == 1
        assert body["Resources"][0]["userName"] == "bob@example.com"

    def test_filter_by_active(self, client: TestClient) -> None:
        _create_user(client, user_name="alice@example.com")
        created = _create_user(client, user_name="bob@example.com")
        client.patch(
            f"/scim/v2/Users/{created['id']}", json=_deactivate_body(), headers=_hdrs()
        )
        resp = client.get(
            "/scim/v2/Users", params={"filter": "active eq false"}, headers=_hdrs()
        )
        body = resp.json()
        assert body["totalResults"] == 1
        assert body["Resources"][0]["userName"] == "bob@example.com"

    def test_unsupported_filter_400_invalid_filter(self, client: TestClient) -> None:
        resp = client.get(
            "/scim/v2/Users",
            params={"filter": 'emails co "example.com"'},
            headers=_hdrs(),
        )
        assert resp.status_code == 400
        assert resp.json()["scimType"] == "invalidFilter"

    def test_quoted_active_filter_value_400(self, client: TestClient) -> None:
        resp = client.get(
            "/scim/v2/Users", params={"filter": 'active eq "yes"'}, headers=_hdrs()
        )
        assert resp.status_code == 400
        assert resp.json()["scimType"] == "invalidFilter"

    def test_non_integer_pagination_400(self, client: TestClient) -> None:
        resp = client.get(
            "/scim/v2/Users", params={"startIndex": "abc"}, headers=_hdrs()
        )
        assert resp.status_code == 400

    def test_pagination(self, client: TestClient) -> None:
        for i in range(3):
            _create_user(client, user_name=f"user{i}@example.com")
        resp = client.get(
            "/scim/v2/Users",
            params={"startIndex": "2", "count": "1"},
            headers=_hdrs(),
        )
        body = resp.json()
        assert body["totalResults"] == 3
        assert body["startIndex"] == 2
        assert body["itemsPerPage"] == 1
        assert len(body["Resources"]) == 1


# ---------------------------------------------------------------------------
# Deactivation / deletion — honest role revocation + audit
# ---------------------------------------------------------------------------


class TestDeprovisioning:
    def test_patch_active_false_revokes_roles_and_audits(
        self, client: TestClient
    ) -> None:
        created = _create_user(client)
        rbac_store.assign_role("alice@example.com", "writer", "test")
        rbac_store.assign_role("alice@example.com", "auditor", "test")

        resp = client.patch(
            f"/scim/v2/Users/{created['id']}", json=_deactivate_body(), headers=_hdrs()
        )
        assert resp.status_code == 200
        assert resp.json()["active"] is False
        assert rbac_store.get_roles("alice@example.com") == []

        events = scim_store.list_audit_events(subject="alice@example.com")
        assert [e["operation"] for e in events] == ["user.create", "user.deactivate"]
        deactivate = events[-1]
        assert deactivate["roles_before"] == ["auditor", "writer"]
        assert deactivate["roles_after"] == []
        assert deactivate["actor"].startswith("scim-token:")

    def test_deactivate_entra_dialect_no_path(self, client: TestClient) -> None:
        """Entra ID sends op=Replace with no path and a value object."""
        created = _create_user(client)
        resp = client.patch(
            f"/scim/v2/Users/{created['id']}",
            json={
                "schemas": [PATCH_OP_URN],
                "Operations": [{"op": "Replace", "value": {"active": "False"}}],
            },
            headers=_hdrs(),
        )
        assert resp.status_code == 200
        assert resp.json()["active"] is False

    def test_last_admin_deactivation_refused_409(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("NOVA_OIDC_ISSUER", raising=False)
        created = _create_user(client, user_name="solo@example.com")
        rbac_store.assign_role("solo@example.com", "admin", "test")

        resp = client.patch(
            f"/scim/v2/Users/{created['id']}", json=_deactivate_body(), headers=_hdrs()
        )
        assert resp.status_code == 409
        body = resp.json()
        assert body["schemas"] == [ERROR_URN]
        assert "last admin" in body["detail"].lower()
        # Nothing mutated: role kept, user still active, no deactivate audit event
        assert rbac_store.get_roles("solo@example.com") == ["admin"]
        stored = scim_store.get_user(created["id"])
        assert stored is not None and stored.active is True
        events = scim_store.list_audit_events(subject="solo@example.com")
        assert [e["operation"] for e in events] == ["user.create"]

    def test_last_admin_delete_refused_409(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("NOVA_OIDC_ISSUER", raising=False)
        created = _create_user(client, user_name="solo@example.com")
        rbac_store.assign_role("solo@example.com", "admin", "test")

        resp = client.delete(f"/scim/v2/Users/{created['id']}", headers=_hdrs())
        assert resp.status_code == 409
        assert scim_store.get_user(created["id"]) is not None

    def test_deactivate_allowed_when_another_admin_remains(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("NOVA_OIDC_ISSUER", raising=False)
        created = _create_user(client, user_name="one@example.com")
        rbac_store.assign_role("one@example.com", "admin", "test")
        rbac_store.assign_role("two@example.com", "admin", "test")

        resp = client.patch(
            f"/scim/v2/Users/{created['id']}", json=_deactivate_body(), headers=_hdrs()
        )
        assert resp.status_code == 200
        assert rbac_store.get_roles("one@example.com") == []
        assert rbac_store.get_roles("two@example.com") == ["admin"]

    def test_delete_removes_user_and_audits(self, client: TestClient) -> None:
        created = _create_user(client)
        rbac_store.assign_role("alice@example.com", "reader", "test")
        resp = client.delete(f"/scim/v2/Users/{created['id']}", headers=_hdrs())
        assert resp.status_code == 204
        assert scim_store.get_user(created["id"]) is None
        assert rbac_store.get_roles("alice@example.com") == []
        events = scim_store.list_audit_events(subject="alice@example.com")
        assert events[-1]["operation"] == "user.delete"
        assert events[-1]["roles_before"] == ["reader"]

    def test_delete_missing_returns_404(self, client: TestClient) -> None:
        resp = client.delete("/scim/v2/Users/nonexistent", headers=_hdrs())
        assert resp.status_code == 404

    def test_reactivation_is_user_update(self, client: TestClient) -> None:
        created = _create_user(client)
        client.patch(
            f"/scim/v2/Users/{created['id']}", json=_deactivate_body(), headers=_hdrs()
        )
        resp = client.patch(
            f"/scim/v2/Users/{created['id']}",
            json={
                "schemas": [PATCH_OP_URN],
                "Operations": [{"op": "replace", "path": "active", "value": True}],
            },
            headers=_hdrs(),
        )
        assert resp.status_code == 200
        assert resp.json()["active"] is True
        events = scim_store.list_audit_events(subject="alice@example.com")
        assert [e["operation"] for e in events] == [
            "user.create",
            "user.deactivate",
            "user.update",
        ]

    def test_patch_display_name(self, client: TestClient) -> None:
        created = _create_user(client)
        resp = client.patch(
            f"/scim/v2/Users/{created['id']}",
            json={
                "schemas": [PATCH_OP_URN],
                "Operations": [
                    {"op": "replace", "path": "displayName", "value": "Alice N."}
                ],
            },
            headers=_hdrs(),
        )
        assert resp.status_code == 200
        assert resp.json()["displayName"] == "Alice N."

    def test_patch_external_id(self, client: TestClient) -> None:
        created = _create_user(client)
        resp = client.patch(
            f"/scim/v2/Users/{created['id']}",
            json={
                "schemas": [PATCH_OP_URN],
                "Operations": [
                    {"op": "replace", "path": "externalId", "value": "okta-new"}
                ],
            },
            headers=_hdrs(),
        )
        assert resp.status_code == 200
        assert resp.json()["externalId"] == "okta-new"

    def test_malformed_patch_json_body_400(self, client: TestClient) -> None:
        created = _create_user(client)
        resp = client.patch(
            f"/scim/v2/Users/{created['id']}",
            content=b"{not json",
            headers={**_hdrs(), "Content-Type": "application/scim+json"},
        )
        assert resp.status_code == 400

    @pytest.mark.parametrize(
        "body",
        [
            {"schemas": ["urn:wrong"], "Operations": [{"op": "replace", "path": "active", "value": False}]},
            {"schemas": [PATCH_OP_URN], "Operations": []},
            {"schemas": [PATCH_OP_URN], "Operations": [{"op": "remove", "path": "active"}]},
            {"schemas": [PATCH_OP_URN], "Operations": [{"op": "replace", "path": "userName", "value": "x"}]},
            {"schemas": [PATCH_OP_URN], "Operations": [{"op": "replace", "path": "active", "value": "maybe"}]},
            {"schemas": [PATCH_OP_URN], "Operations": ["not-an-object"]},
            {"schemas": [PATCH_OP_URN], "Operations": [{"op": "replace", "value": "no-path-non-object"}]},
            {"schemas": [PATCH_OP_URN], "Operations": [{"op": "replace", "value": {"userName": "x"}}]},
            {"schemas": [PATCH_OP_URN], "Operations": [{"op": "replace", "path": "externalId", "value": 42}]},
            ["not", "an", "object"],
        ],
    )
    def test_malformed_patch_400(self, client: TestClient, body: Any) -> None:
        created = _create_user(client)
        resp = client.patch(
            f"/scim/v2/Users/{created['id']}", json=body, headers=_hdrs()
        )
        assert resp.status_code == 400
        assert resp.json()["schemas"] == [ERROR_URN]


# ---------------------------------------------------------------------------
# Store-level unit tests
# ---------------------------------------------------------------------------


class TestScimStore:
    def test_duplicate_username_raises(self, tmp_path: Path) -> None:
        db = tmp_path / "store.db"
        scim_store.create_user("dup@example.com", db_path=db)
        with pytest.raises(scim_store.DuplicateUserNameError):
            scim_store.create_user("dup@example.com", db_path=db)

    def test_update_unknown_column_rejected(self, tmp_path: Path) -> None:
        db = tmp_path / "store.db"
        user = scim_store.create_user("u@example.com", db_path=db)
        with pytest.raises(ValueError):
            scim_store.update_user(user.id, changes={"user_name": "x"}, db_path=db)

    def test_update_missing_user_returns_none(self, tmp_path: Path) -> None:
        db = tmp_path / "store.db"
        assert scim_store.update_user("ghost", changes={"active": False}, db_path=db) is None

    def test_audit_events_filter_by_subject(self, tmp_path: Path) -> None:
        db = tmp_path / "store.db"
        for subject in ("a@example.com", "b@example.com"):
            scim_store.append_audit_event(
                actor="scim-token:abc",
                operation="user.create",
                resource_type="User",
                subject=subject,
                roles_before=[],
                roles_after=[],
                db_path=db,
            )
        events = scim_store.list_audit_events(subject="a@example.com", db_path=db)
        assert len(events) == 1
        assert events[0]["subject"] == "a@example.com"
        all_events = scim_store.list_audit_events(db_path=db)
        assert len(all_events) == 2
