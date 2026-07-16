"""ADR-0139 D3 / ADR-0190 — SCIM /scim/v2/Groups route-wiring (backlog A6).

Drives the real FastAPI app: creating a group grants the mapped RBAC role to its
members; removing a member (or deleting the group) revokes only SCIM-owned roles;
manual grants and unmapped groups are respected; the last-admin guard holds.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from novafabric.server import rbac_store  # noqa: E402
from novafabric.server.app import create_app  # noqa: E402
from novafabric.server.config import ScimConfig, ServerConfig  # noqa: E402

SCIM_TOKEN = "test-provisioning-token"
USER_URN = "urn:ietf:params:scim:schemas:core:2.0:User"
GROUP_URN = "urn:ietf:params:scim:schemas:core:2.0:Group"
PATCH_OP_URN = "urn:ietf:params:scim:api:messages:2.0:PatchOp"

_MAP = {"Engineering": "writer", "SRE-Admins": "admin"}


def _make_client(tmp_path: Path) -> TestClient:
    cfg = ServerConfig(
        db_path=str(tmp_path / "scim.db"),
        scim=ScimConfig(enabled=True, group_role_map=_MAP),
    )
    cfg.scim_token = SCIM_TOKEN
    return TestClient(create_app(cfg), raise_server_exceptions=False)


def _hdrs() -> dict[str, str]:
    return {"Authorization": f"Bearer {SCIM_TOKEN}"}


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    return _make_client(tmp_path)


def _create_user(client: TestClient, user_name: str) -> dict[str, Any]:
    resp = client.post(
        "/scim/v2/Users",
        json={"schemas": [USER_URN], "userName": user_name, "active": True},
        headers=_hdrs(),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _create_group(
    client: TestClient, display_name: str, member_ids: list[str]
) -> dict[str, Any]:
    resp = client.post(
        "/scim/v2/Groups",
        json={
            "schemas": [GROUP_URN],
            "displayName": display_name,
            "members": [{"value": mid} for mid in member_ids],
        },
        headers=_hdrs(),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_groups_disabled_returns_404(tmp_path: Path) -> None:
    cfg = ServerConfig(db_path=str(tmp_path / "scim.db"))  # scim disabled
    c = TestClient(create_app(cfg), raise_server_exceptions=False)
    assert c.get("/scim/v2/Groups", headers=_hdrs()).status_code == 404


def test_create_group_grants_mapped_role_to_members(client: TestClient) -> None:
    user = _create_user(client, "alice@example.com")
    group = _create_group(client, "Engineering", [user["id"]])

    assert group["displayName"] == "Engineering"
    assert "writer" in rbac_store.get_roles("alice@example.com")
    assert (
        rbac_store.get_assigned_by("alice@example.com", "writer")
        == "scim:group"
    )


def test_unmapped_group_grants_no_role(client: TestClient) -> None:
    user = _create_user(client, "bob@example.com")
    _create_group(client, "Some-Unmapped-Group", [user["id"]])
    assert rbac_store.get_roles("bob@example.com") == []


def test_patch_remove_member_revokes_the_scim_role(client: TestClient) -> None:
    user = _create_user(client, "carol@example.com")
    group = _create_group(client, "Engineering", [user["id"]])
    assert "writer" in rbac_store.get_roles("carol@example.com")

    resp = client.patch(
        f"/scim/v2/Groups/{group['id']}",
        json={
            "schemas": [PATCH_OP_URN],
            "Operations": [{"op": "remove", "path": f'members[value eq "{user["id"]}"]'}],
        },
        headers=_hdrs(),
    )
    assert resp.status_code == 200, resp.text
    assert "writer" not in rbac_store.get_roles("carol@example.com")


def test_delete_group_revokes_member_roles(client: TestClient) -> None:
    user = _create_user(client, "dan@example.com")
    group = _create_group(client, "Engineering", [user["id"]])
    assert "writer" in rbac_store.get_roles("dan@example.com")

    assert client.delete(f"/scim/v2/Groups/{group['id']}", headers=_hdrs()).status_code == 204
    assert "writer" not in rbac_store.get_roles("dan@example.com")


def test_get_group_returns_resource(client: TestClient) -> None:
    user = _create_user(client, "erin@example.com")
    group = _create_group(client, "Engineering", [user["id"]])
    resp = client.get(f"/scim/v2/Groups/{group['id']}", headers=_hdrs())
    assert resp.status_code == 200
    body = resp.json()
    assert body["displayName"] == "Engineering"
    assert body["members"][0]["value"] == user["id"]


# ---------------------------------------------------------------------------
# PUT /scim/v2/Groups/{id} — RFC 7644 §3.5.1 full replace (ADR-0139 leftover)
# ---------------------------------------------------------------------------


def _put_group(
    client: TestClient,
    group_id: str,
    display_name: str,
    member_ids: list[str],
) -> Any:
    return client.put(
        f"/scim/v2/Groups/{group_id}",
        json={
            "schemas": [GROUP_URN],
            "displayName": display_name,
            "members": [{"value": mid} for mid in member_ids],
        },
        headers=_hdrs(),
    )


def test_put_replaces_members_and_reconciles_roles(client: TestClient) -> None:
    alice = _create_user(client, "put-alice@example.com")
    bob = _create_user(client, "put-bob@example.com")
    group = _create_group(client, "Engineering", [alice["id"]])
    assert "writer" in rbac_store.get_roles("put-alice@example.com")

    resp = _put_group(client, group["id"], "Engineering", [bob["id"]])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [m["value"] for m in body["members"]] == [bob["id"]]
    # Removed member loses the SCIM-owned grant; added member gains it.
    assert "writer" not in rbac_store.get_roles("put-alice@example.com")
    assert "writer" in rbac_store.get_roles("put-bob@example.com")
    assert rbac_store.get_assigned_by("put-bob@example.com", "writer") == "scim:group"


def test_put_preserves_manual_grant_of_removed_member(client: TestClient) -> None:
    carol = _create_user(client, "put-carol@example.com")
    rbac_store.assign_role("put-carol@example.com", "writer", "operator-admin")
    group = _create_group(client, "Engineering", [carol["id"]])

    resp = _put_group(client, group["id"], "Engineering", [])
    assert resp.status_code == 200, resp.text
    # The operator's grant survives — SCIM never owned that row (ADR-0190).
    assert "writer" in rbac_store.get_roles("put-carol@example.com")
    assert (
        rbac_store.get_assigned_by("put-carol@example.com", "writer")
        == "operator-admin"
    )


def test_put_removing_last_admin_member_refused_409(client: TestClient) -> None:
    dana = _create_user(client, "put-dana@example.com")
    group = _create_group(client, "SRE-Admins", [dana["id"]])
    assert "admin" in rbac_store.get_roles("put-dana@example.com")

    resp = _put_group(client, group["id"], "SRE-Admins", [])
    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["scimType"] == "mutability"
    # ADR-0060: the refused revoke leaves the admin role in place.
    assert "admin" in rbac_store.get_roles("put-dana@example.com")


def test_put_display_name_rename_remaps_member_roles(client: TestClient) -> None:
    erin = _create_user(client, "put-erin@example.com")
    other_admin = _create_user(client, "put-other-admin@example.com")
    _create_group(client, "SRE-Admins", [other_admin["id"]])  # keeps an admin around
    group = _create_group(client, "Engineering", [erin["id"]])
    assert "writer" in rbac_store.get_roles("put-erin@example.com")

    resp = _put_group(client, group["id"], "SRE-Admins", [erin["id"]])
    assert resp.status_code == 200, resp.text
    assert resp.json()["displayName"] == "SRE-Admins"
    roles = rbac_store.get_roles("put-erin@example.com")
    assert "admin" in roles
    assert "writer" not in roles


def test_put_rename_to_unmapped_group_revokes_scim_role(client: TestClient) -> None:
    frank = _create_user(client, "put-frank@example.com")
    group = _create_group(client, "Engineering", [frank["id"]])
    resp = _put_group(client, group["id"], "Some-Unmapped-Group", [frank["id"]])
    assert resp.status_code == 200, resp.text
    assert "writer" not in rbac_store.get_roles("put-frank@example.com")


def test_put_missing_group_returns_404(client: TestClient) -> None:
    resp = _put_group(client, "does-not-exist", "Engineering", [])
    assert resp.status_code == 404
    assert resp.json()["schemas"] == ["urn:ietf:params:scim:api:messages:2.0:Error"]


def test_put_malformed_payload_400(client: TestClient) -> None:
    user = _create_user(client, "put-gail@example.com")
    group = _create_group(client, "Engineering", [user["id"]])
    resp = client.put(
        f"/scim/v2/Groups/{group['id']}",
        json={"schemas": [GROUP_URN]},  # missing displayName
        headers=_hdrs(),
    )
    assert resp.status_code == 400
    assert resp.json()["scimType"] == "invalidValue"
    # Nothing mutated: membership and the member's role are untouched.
    assert "writer" in rbac_store.get_roles("put-gail@example.com")


def test_deleting_group_preserves_a_manually_granted_role(client: TestClient) -> None:
    user = _create_user(client, "frank@example.com")
    # Operator grants writer directly, independent of SCIM.
    rbac_store.assign_role("frank@example.com", "writer", "operator-admin")
    group = _create_group(client, "Engineering", [user["id"]])  # SCIM also maps writer
    client.delete(f"/scim/v2/Groups/{group['id']}", headers=_hdrs())
    # The operator's grant survives — SCIM never owned that row.
    assert "writer" in rbac_store.get_roles("frank@example.com")
    assert (
        rbac_store.get_assigned_by("frank@example.com", "writer")
        == "operator-admin"
    )
