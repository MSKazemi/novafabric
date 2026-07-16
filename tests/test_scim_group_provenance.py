"""ADR-0190 — SCIM group→role reconcile provenance (backlog A6 route-wiring core).

SCIM grants/revokes RBAC roles from group membership, but only ever touches the
rows it owns (``assigned_by == "scim:group"``). Manual / OIDC grants are never
overwritten or revoked; the ADR-0060 last-admin guard is preserved.
"""
from __future__ import annotations

import pytest

from novafabric.server import rbac_store, scim_store


def test_reconcile_grants_scim_role_with_scim_provenance(tmp_path):
    db = tmp_path / "r.db"
    change = scim_store.reconcile_subject_roles(
        subject="alice", desired_roles={"writer"}, actor="scim-token:x", db_path=db
    )
    assert change.granted == {"writer"}
    assert "writer" in rbac_store.get_roles("alice", db_path=db)
    assert rbac_store.get_assigned_by("alice", "writer", db_path=db) == "scim:group"


def test_reconcile_revokes_a_role_it_previously_granted(tmp_path):
    db = tmp_path / "r.db"
    scim_store.reconcile_subject_roles(
        subject="bob", desired_roles={"writer"}, actor="scim", db_path=db
    )
    change = scim_store.reconcile_subject_roles(
        subject="bob", desired_roles=set(), actor="scim", db_path=db
    )
    assert change.revoked == {"writer"}
    assert "writer" not in rbac_store.get_roles("bob", db_path=db)


def test_reconcile_never_revokes_a_manually_granted_role(tmp_path):
    db = tmp_path / "r.db"
    # An operator grants writer directly (not via SCIM).
    rbac_store.assign_role("carol", "writer", "operator-admin", db_path=db)
    # SCIM has no intent for carol; reconciling to empty must leave the manual row.
    scim_store.reconcile_subject_roles(
        subject="carol", desired_roles=set(), actor="scim", db_path=db
    )
    assert "writer" in rbac_store.get_roles("carol", db_path=db)
    assert rbac_store.get_assigned_by("carol", "writer", db_path=db) == "operator-admin"


def test_reconcile_does_not_overwrite_an_existing_manual_role(tmp_path):
    db = tmp_path / "r.db"
    rbac_store.assign_role("dave", "writer", "operator-admin", db_path=db)
    # SCIM *wants* writer for dave, but an operator already owns that row — SCIM must
    # not seize it (provenance stays manual) and must not later revoke it.
    scim_store.reconcile_subject_roles(
        subject="dave", desired_roles={"writer"}, actor="scim", db_path=db
    )
    assert rbac_store.get_assigned_by("dave", "writer", db_path=db) == "operator-admin"
    scim_store.reconcile_subject_roles(
        subject="dave", desired_roles=set(), actor="scim", db_path=db
    )
    assert "writer" in rbac_store.get_roles("dave", db_path=db)  # preserved


def test_reconcile_role_change_writes_one_audit_event(tmp_path):
    db = tmp_path / "r.db"
    scim_store.reconcile_subject_roles(
        subject="erin", desired_roles={"writer"}, actor="scim", db_path=db
    )
    events = scim_store.list_audit_events(db_path=db)
    assert len(events) == 1
    assert events[0]["subject"] == "erin"
    assert events[0]["roles_after"] == ["writer"]


def test_reconcile_no_change_writes_no_audit_event(tmp_path):
    db = tmp_path / "r.db"
    scim_store.reconcile_subject_roles(
        subject="frank", desired_roles={"writer"}, actor="scim", db_path=db
    )
    before = len(scim_store.list_audit_events(db_path=db))
    scim_store.reconcile_subject_roles(
        subject="frank", desired_roles={"writer"}, actor="scim", db_path=db
    )
    assert len(scim_store.list_audit_events(db_path=db)) == before


def test_reconcile_refuses_to_revoke_last_admin(tmp_path, monkeypatch):
    db = tmp_path / "r.db"
    monkeypatch.delenv("NOVA_OIDC_ISSUER", raising=False)
    # SCIM granted the only admin, then a group change would revoke it.
    scim_store.reconcile_subject_roles(
        subject="gina", desired_roles={"admin"}, actor="scim", db_path=db
    )
    with pytest.raises(rbac_store.LastAdminError):
        scim_store.reconcile_subject_roles(
            subject="gina", desired_roles=set(), actor="scim", db_path=db
        )
    # The refused revoke left the admin row intact (no partial mutation).
    assert "admin" in rbac_store.get_roles("gina", db_path=db)
