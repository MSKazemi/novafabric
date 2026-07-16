"""ADR-0139 D3 — SCIM Group -> RBAC role mapping (backlog A6 core slice).

Config-driven mapping from IdP group ``displayName`` to one of the six RBAC roles.
A user in a mapped group gets that role; removal revokes it; unmapped groups grant
nothing; a role change emits one append-only audit event. Maker-checker SoD
(ADR-0058) is preserved — a subject may hold both promoter and approver.
"""
from __future__ import annotations

import pytest

from novafabric.server.scim_group_mapping import (
    GroupRoleMapping,
    UnknownRoleError,
    apply_group_membership,
    resolve_roles,
)


def _mapping() -> GroupRoleMapping:
    return GroupRoleMapping.from_config(
        {
            "Engineering": "writer",
            "SRE-Admins": "admin",
            "Compliance": "auditor",
            "Release-Makers": "promoter",
            "Release-Checkers": "approver",
        }
    )


def test_mapped_group_grants_its_role():
    roles = resolve_roles(["Engineering"], _mapping())
    assert roles == {"writer"}


def test_unmapped_group_grants_no_role():
    # SCIM requires the server to accept valid groups it cannot map — they simply
    # grant nothing (never an error, never a default role).
    roles = resolve_roles(["Some-Random-IdP-Group"], _mapping())
    assert roles == set()


def test_user_with_no_groups_gets_no_roles():
    # A non-group user is unaffected — RBAC is not altered for them.
    assert resolve_roles([], _mapping()) == set()


def test_membership_across_multiple_groups_unions_roles():
    roles = resolve_roles(["Engineering", "Compliance"], _mapping())
    assert roles == {"writer", "auditor"}


def test_maker_checker_sod_subject_may_hold_both_promoter_and_approver():
    # ADR-0058 SoD is preserved at the *proposal* level, not by refusing to grant
    # both roles — resolution may legitimately grant both to one identity.
    roles = resolve_roles(["Release-Makers", "Release-Checkers"], _mapping())
    assert roles == {"promoter", "approver"}


def test_unknown_role_in_config_is_rejected():
    with pytest.raises(UnknownRoleError):
        GroupRoleMapping.from_config({"Engineering": "superuser"})


def test_group_name_is_matched_after_stripping_surrounding_whitespace():
    mapping = GroupRoleMapping.from_config({"  Engineering  ": "writer"})
    assert resolve_roles(["Engineering"], mapping) == {"writer"}


def test_apply_group_membership_grant_emits_one_audit_event(tmp_path):
    db = tmp_path / "scim.db"
    change = apply_group_membership(
        actor="scim-bridge",
        subject="alice",
        groups_before=[],
        groups_after=["Engineering"],
        mapping=_mapping(),
        db_path=db,
    )
    assert change.before == set()
    assert change.after == {"writer"}
    assert change.granted == {"writer"}
    assert change.revoked == set()

    from novafabric.server.scim_store import list_audit_events

    events = list_audit_events(db_path=db)
    assert len(events) == 1
    ev = events[0]
    assert ev["subject"] == "alice"
    assert ev["operation"] == "group-role-remap"
    assert ev["roles_before"] == []
    assert ev["roles_after"] == ["writer"]


def test_apply_group_membership_removal_revokes_and_audits(tmp_path):
    db = tmp_path / "scim.db"
    change = apply_group_membership(
        actor="scim-bridge",
        subject="bob",
        groups_before=["SRE-Admins"],
        groups_after=[],
        mapping=_mapping(),
        db_path=db,
    )
    assert change.before == {"admin"}
    assert change.after == set()
    assert change.revoked == {"admin"}

    from novafabric.server.scim_store import list_audit_events

    events = list_audit_events(db_path=db)
    assert len(events) == 1
    assert events[0]["roles_before"] == ["admin"]
    assert events[0]["roles_after"] == []


def test_apply_group_membership_no_change_emits_no_audit_event(tmp_path):
    db = tmp_path / "scim.db"
    apply_group_membership(
        actor="scim-bridge",
        subject="carol",
        groups_before=["Engineering"],
        groups_after=["Engineering"],
        mapping=_mapping(),
        db_path=db,
    )
    from novafabric.server.scim_store import list_audit_events

    assert list_audit_events(db_path=db) == []


# ---------------------------------------------------------------------------
# ServerConfig integration (ADR-0029): operators declare the map in config.
# ---------------------------------------------------------------------------


def test_scim_config_accepts_group_role_map_and_builds_a_mapping():
    from novafabric.server.config import ScimConfig

    cfg = ScimConfig(
        enabled=True,
        group_role_map={"Engineering": "writer", "SRE-Admins": "admin"},
    )
    mapping = cfg.role_mapping()
    assert resolve_roles(["Engineering"], mapping) == {"writer"}
    assert resolve_roles(["SRE-Admins"], mapping) == {"admin"}


def test_scim_config_rejects_unknown_role_in_map():
    import pydantic

    from novafabric.server.config import ScimConfig

    with pytest.raises(pydantic.ValidationError):
        ScimConfig(group_role_map={"Engineering": "superuser"})


def test_scim_config_default_group_role_map_is_empty_and_grants_nothing():
    from novafabric.server.config import ScimConfig

    cfg = ScimConfig()
    assert cfg.group_role_map == {}
    assert resolve_roles(["Engineering"], cfg.role_mapping()) == set()
