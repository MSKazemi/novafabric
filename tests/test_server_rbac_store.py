"""Tests for role_assignments store (Track S-3 + DA-1.3 v0.14)."""

from __future__ import annotations

from pathlib import Path

import pytest

from novafabric.server.rbac_store import (
    LastAdminError,
    assign_role,
    get_roles,
    list_assignments,
    list_subjects,
    revoke_role,
)


class TestRbacStore:
    def test_assign_and_get_role(self, tmp_path: Path) -> None:
        db = tmp_path / "registry.db"
        # Initialise the DB by touching it via the registry store
        from novafabric.registry.store import get_connection, init_schema

        conn = get_connection(db)
        init_schema(conn)
        conn.close()

        assign_role("alice@example.com", "writer", "cli", db_path=db)
        roles = get_roles("alice@example.com", db_path=db)
        assert "writer" in roles

    def test_assign_multiple_roles(self, tmp_path: Path) -> None:
        db = tmp_path / "registry.db"
        from novafabric.registry.store import get_connection, init_schema

        conn = get_connection(db)
        init_schema(conn)
        conn.close()

        assign_role("bob@example.com", "reader", "admin", db_path=db)
        assign_role("bob@example.com", "auditor", "admin", db_path=db)
        roles = get_roles("bob@example.com", db_path=db)
        assert "reader" in roles
        assert "auditor" in roles

    def test_get_roles_empty_for_unknown_user(self, tmp_path: Path) -> None:
        db = tmp_path / "registry.db"
        from novafabric.registry.store import get_connection, init_schema

        conn = get_connection(db)
        init_schema(conn)
        conn.close()

        roles = get_roles("nobody@example.com", db_path=db)
        assert roles == []

    def test_list_assignments(self, tmp_path: Path) -> None:
        db = tmp_path / "registry.db"
        from novafabric.registry.store import get_connection, init_schema

        conn = get_connection(db)
        init_schema(conn)
        conn.close()

        assign_role("carol@example.com", "admin", "superuser", db_path=db)
        assignments = list_assignments(db_path=db)
        assert any(
            a["subject"] == "carol@example.com" and a["role"] == "admin"
            for a in assignments
        )

    def test_assign_role_idempotent(self, tmp_path: Path) -> None:
        db = tmp_path / "registry.db"
        from novafabric.registry.store import get_connection, init_schema

        conn = get_connection(db)
        init_schema(conn)
        conn.close()

        assign_role("dan@example.com", "reader", "cli", db_path=db)
        assign_role("dan@example.com", "reader", "cli", db_path=db)  # second assign OK
        roles = get_roles("dan@example.com", db_path=db)
        assert roles.count("reader") == 1  # no duplicate


def _bootstrap_db(db: Path) -> None:
    """Initialise the registry schema so subsequent rbac_store calls work."""
    from novafabric.registry.store import get_connection, init_schema

    conn = get_connection(db)
    init_schema(conn)
    conn.close()


class TestRevokeRole:
    """ADR-0060: revoke + lockout invariant."""

    def test_revoke_removes_assignment(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("NOVA_OIDC_ISSUER", raising=False)
        db = tmp_path / "registry.db"
        _bootstrap_db(db)
        assign_role("alice@example.com", "writer", "admin", db_path=db)
        assign_role("admin@example.com", "admin", "system", db_path=db)

        deleted = revoke_role("alice@example.com", "writer", db_path=db)
        assert deleted is True
        assert "writer" not in get_roles("alice@example.com", db_path=db)

    def test_revoke_missing_returns_false(self, tmp_path: Path) -> None:
        db = tmp_path / "registry.db"
        _bootstrap_db(db)
        assert revoke_role("ghost@example.com", "reader", db_path=db) is False

    def test_revoke_last_admin_blocked_without_oidc(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("NOVA_OIDC_ISSUER", raising=False)
        db = tmp_path / "registry.db"
        _bootstrap_db(db)
        assign_role("solo@example.com", "admin", "system", db_path=db)

        with pytest.raises(LastAdminError):
            revoke_role("solo@example.com", "admin", db_path=db)
        # Assignment must still be present after the failed revoke
        assert "admin" in get_roles("solo@example.com", db_path=db)

    def test_revoke_last_admin_allowed_with_oidc(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NOVA_OIDC_ISSUER", "https://issuer.example.com")
        db = tmp_path / "registry.db"
        _bootstrap_db(db)
        assign_role("solo@example.com", "admin", "system", db_path=db)

        deleted = revoke_role("solo@example.com", "admin", db_path=db)
        assert deleted is True

    def test_revoke_admin_allowed_when_another_admin_remains(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("NOVA_OIDC_ISSUER", raising=False)
        db = tmp_path / "registry.db"
        _bootstrap_db(db)
        assign_role("alice@example.com", "admin", "system", db_path=db)
        assign_role("bob@example.com", "admin", "system", db_path=db)

        deleted = revoke_role("alice@example.com", "admin", db_path=db)
        assert deleted is True
        # Bob still admin
        assert "admin" in get_roles("bob@example.com", db_path=db)

    def test_revoke_non_admin_role_never_blocked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("NOVA_OIDC_ISSUER", raising=False)
        db = tmp_path / "registry.db"
        _bootstrap_db(db)
        assign_role("alice@example.com", "writer", "system", db_path=db)

        # No admins exist at all, but we're revoking writer, not admin
        deleted = revoke_role("alice@example.com", "writer", db_path=db)
        assert deleted is True


class TestListSubjects:
    def test_empty_when_no_assignments(self, tmp_path: Path) -> None:
        db = tmp_path / "registry.db"
        _bootstrap_db(db)
        assert list_subjects(db_path=db) == []

    def test_distinct_subjects_ordered(self, tmp_path: Path) -> None:
        db = tmp_path / "registry.db"
        _bootstrap_db(db)
        assign_role("c@example.com", "writer", "admin", db_path=db)
        assign_role("a@example.com", "reader", "admin", db_path=db)
        assign_role("a@example.com", "writer", "admin", db_path=db)  # same subject
        assign_role("b@example.com", "admin", "admin", db_path=db)

        subjects = list_subjects(db_path=db)
        assert subjects == ["a@example.com", "b@example.com", "c@example.com"]
