"""H6 — ``nova server assign-role`` must not report an assignment that has no effect.

``server/rbac.py`` consults ``role_assignments`` rows only once the ADR-0178
workspace/org model is in use (a scoped membership or a service account
exists). That gate is deliberate (ADR-0178 §5 "zero behaviour change"; the
per-request merge ADR-0060 deferred). A stock deployment has neither, so a row
written by ``assign-role`` grants nothing — yet the CLI used to print
"Role 'writer' assigned to 'alice'" and stop. These tests pin that every
surface reporting an assignment says whether it is in force *now*:

* the CLI (stdout + a stderr warning when it is not in force);
* ``GET /v0/admin/roles`` ``effective_now`` (which keyed on the env vars
  ``NOVA_OIDC_ISSUER``/``NOVA_OIDC_CLIENT_ID`` that server-mode OIDC never reads);
* and that the warning flips off as soon as the rows ARE consulted.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from novafabric.cli.main import app
from novafabric.server import rbac, rbac_store, workspace_store

runner = CliRunner()


def _invoke_assign(db_path: Path, *extra: str) -> object:
    return runner.invoke(
        app,
        ["server", "assign-role", "alice@example.com", "writer", "--db-path", str(db_path), *extra],
    )


def test_assignment_not_in_force_on_a_stock_store(tmp_path: Path) -> None:
    db = tmp_path / "registry.db"
    assert rbac.role_assignments_in_force(db_path=db) is False


def test_assignment_in_force_once_a_membership_exists(tmp_path: Path) -> None:
    db = tmp_path / "registry.db"
    org = workspace_store.create_org("acme", "Acme", "admin@example.com", db_path=db)
    workspace_store.add_membership("bob@example.com", "org", org["id"], "reader", "test", db_path=db)
    assert rbac.role_assignments_in_force(db_path=db) is True


def test_in_force_check_fails_closed_to_not_in_force(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(**_kw: object) -> bool:
        raise RuntimeError("store unreadable")

    monkeypatch.setattr(workspace_store, "feature_in_use", _boom)
    assert rbac.role_assignments_in_force(db_path=tmp_path / "r.db") is False


def test_cli_warns_on_stderr_when_assignment_has_no_effect(tmp_path: Path) -> None:
    db = tmp_path / "registry.db"
    result = _invoke_assign(db)
    assert isinstance(result.exception, SystemExit) or result.exception is None
    assert result.exit_code == 0, result.output
    # The row IS written (the lockout invariant and a later workspace roll-out use it) …
    assert rbac_store.get_roles("alice@example.com", db_path=db) == ["writer"]
    # … but nothing claims it took effect.
    assert "assigned to" not in result.stdout
    assert "recorded" in result.stdout
    assert "not in force" in result.stderr
    assert "ADR-0178" in result.stderr
    # The warning says how to actually grant the role today.
    assert "--roles" in result.stderr and "nova_roles" in result.stderr


def test_cli_reports_plain_success_when_assignment_is_in_force(tmp_path: Path) -> None:
    db = tmp_path / "registry.db"
    org = workspace_store.create_org("acme", "Acme", "admin@example.com", db_path=db)
    workspace_store.add_membership("bob@example.com", "org", org["id"], "reader", "test", db_path=db)
    result = _invoke_assign(db)
    assert result.exit_code == 0, result.output
    assert "Role 'writer' assigned to 'alice@example.com'." in result.stdout
    assert "not in force" not in result.stderr


def test_v0_roles_list_reports_effective_now_truthfully(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from novafabric.server.routes import roles as roles_route

    db = tmp_path / "registry.db"
    monkeypatch.setattr(
        rbac_store, "_get_conn", _bound_conn(rbac_store._get_conn, db)
    )
    monkeypatch.setattr(
        workspace_store, "_get_conn", _bound_conn(workspace_store._get_conn, db)
    )
    # Neither env var is read by server-mode OIDC (YAML ``oidc.issuer_url``); they
    # must not decide whether a row is in force.
    monkeypatch.delenv("NOVA_OIDC_ISSUER", raising=False)
    monkeypatch.delenv("NOVA_OIDC_CLIENT_ID", raising=False)
    rbac_store.assign_role("alice@example.com", "writer", "test")

    body = roles_route._list_roles_response(None)
    assert [r["effective_now"] for r in body["roles"]] == [False]
    assert "ADR-0178" in body["message"]

    org = workspace_store.create_org("acme", "Acme", "admin@example.com")
    workspace_store.add_membership("bob@example.com", "org", org["id"], "reader", "test")
    body = roles_route._list_roles_response(None)
    assert [r["effective_now"] for r in body["roles"]] == [True]
    assert body["message"] == ""


def _bound_conn(real: object, db: Path) -> object:
    def _conn(db_path: Path | None = None) -> object:
        return real(db_path or db)  # type: ignore[operator]

    return _conn
