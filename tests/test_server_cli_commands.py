"""Tests for nova server CLI subcommands (Track S-3).

Covers issue-token, revoke-token, assign-role.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()


class TestIssueToken:
    def test_issue_token_prints_jwt(self, tmp_path: Path) -> None:
        key_path = tmp_path / "offline.pem"
        result = runner.invoke(
            app,
            [
                "server", "issue-token",
                "--subject", "test@example.com",
                "--roles", "reader",
                "--expires-in", "7d",
                "--key-path", str(key_path),
            ],
        )
        assert result.exit_code == 0, result.output
        # The last line of output is the JWT; prior lines may be status messages
        token = result.output.strip().splitlines()[-1].strip()
        parts = token.split(".")
        assert len(parts) == 3

    def test_issue_token_creates_keypair_if_missing(self, tmp_path: Path) -> None:
        key_path = tmp_path / "subdir" / "offline.pem"
        assert not key_path.exists()
        result = runner.invoke(
            app,
            [
                "server", "issue-token",
                "--subject", "svc@example.com",
                "--roles", "admin",
                "--key-path", str(key_path),
            ],
        )
        assert result.exit_code == 0, result.output
        assert key_path.exists()

    def test_issued_token_is_verifiable(self, tmp_path: Path) -> None:
        import jwt

        key_path = tmp_path / "offline.pem"
        result = runner.invoke(
            app,
            [
                "server", "issue-token",
                "--subject", "verify@example.com",
                "--roles", "writer",
                "--expires-in", "1d",
                "--key-path", str(key_path),
            ],
        )
        assert result.exit_code == 0
        # Last line is the JWT
        token = result.output.strip().splitlines()[-1].strip()
        claims = jwt.decode(token, options={"verify_signature": False})
        assert claims["sub"] == "verify@example.com"
        assert "writer" in claims["nova_roles"]


class TestRevokeToken:
    def test_revoke_token(self, tmp_path: Path) -> None:
        import jwt

        key_path = tmp_path / "offline.pem"
        # Issue first
        issue_result = runner.invoke(
            app,
            [
                "server", "issue-token",
                "--subject", "revoke-me@example.com",
                "--roles", "reader",
                "--key-path", str(key_path),
            ],
        )
        assert issue_result.exit_code == 0
        # Last line is the JWT
        token = issue_result.output.strip().splitlines()[-1].strip()
        claims = jwt.decode(token, options={"verify_signature": False})
        token_id = claims["jti"]

        # Revoke
        revoke_result = runner.invoke(
            app,
            [
                "server", "revoke-token",
                token_id,
                "--key-path", str(key_path),
            ],
        )
        assert revoke_result.exit_code == 0
        assert "revoked" in revoke_result.output.lower()

    def test_revoke_nonexistent_token(self, tmp_path: Path) -> None:
        key_path = tmp_path / "offline.pem"
        # Generate keypair first (so DB exists)
        from novafabric.server.offline_tokens import generate_keypair, issue_token

        generate_keypair(key_path)
        issue_token("x@example.com", ["reader"], 1, key_path)

        result = runner.invoke(
            app,
            [
                "server", "revoke-token",
                "nonexistent-id",
                "--key-path", str(key_path),
            ],
        )
        assert result.exit_code == 1


class TestHelperFunctions:
    def test_parse_days_with_d_suffix(self) -> None:
        from novafabric.cli.server import _parse_days

        assert _parse_days("90d") == 90
        assert _parse_days("7d") == 7

    def test_parse_days_without_suffix(self) -> None:
        from novafabric.cli.server import _parse_days

        assert _parse_days("30") == 30

    def test_resolve_key_path_from_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from novafabric.cli.server import _resolve_key_path

        expected = tmp_path / "env-key.pem"
        monkeypatch.setenv("NOVAFABRIC_OFFLINE_KEY_PATH", str(expected))
        result = _resolve_key_path(None)
        assert result == expected

    def test_resolve_key_path_from_arg(self, tmp_path: Path) -> None:
        from novafabric.cli.server import _resolve_key_path

        key = tmp_path / "my-key.pem"
        assert _resolve_key_path(key) == key

    def test_resolve_key_path_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from novafabric.cli.server import _resolve_key_path

        monkeypatch.delenv("NOVAFABRIC_OFFLINE_KEY_PATH", raising=False)
        result = _resolve_key_path(None)
        assert "offline-key.pem" in str(result)


class TestAssignRole:
    def test_assign_role(self, tmp_path: Path) -> None:
        db_path = tmp_path / "registry.db"
        from novafabric.registry.store import get_connection, init_schema

        conn = get_connection(db_path)
        init_schema(conn)
        conn.close()

        result = runner.invoke(
            app,
            [
                "server", "assign-role",
                "alice@example.com",
                "writer",
                "--db-path", str(db_path),
            ],
        )
        assert result.exit_code == 0, result.output
        assert "writer" in result.output

    def test_assign_invalid_role(self, tmp_path: Path) -> None:
        result = runner.invoke(
            app,
            [
                "server", "assign-role",
                "alice@example.com",
                "superuser",  # invalid
            ],
        )
        assert result.exit_code == 1


class TestRevokeRole:
    def _bootstrap_db(self, tmp_path: Path) -> Path:
        db_path = tmp_path / "registry.db"
        from novafabric.registry.store import get_connection, init_schema

        conn = get_connection(db_path)
        init_schema(conn)
        conn.close()
        return db_path

    def test_revoke_role(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("NOVA_OIDC_ISSUER", raising=False)
        db_path = self._bootstrap_db(tmp_path)
        # Assign first so there is something to revoke; second admin so lockout
        # invariant does not fire.
        runner.invoke(
            app,
            ["server", "assign-role", "alice@example.com", "writer",
             "--db-path", str(db_path)],
        )
        runner.invoke(
            app,
            ["server", "assign-role", "ops@example.com", "admin",
             "--db-path", str(db_path)],
        )

        result = runner.invoke(
            app,
            ["server", "revoke-role", "alice@example.com", "writer",
             "--db-path", str(db_path)],
        )
        assert result.exit_code == 0, result.output
        assert "revoked" in result.output.lower()

    def test_revoke_missing_returns_1(self, tmp_path: Path) -> None:
        db_path = self._bootstrap_db(tmp_path)
        result = runner.invoke(
            app,
            ["server", "revoke-role", "ghost@example.com", "reader",
             "--db-path", str(db_path)],
        )
        assert result.exit_code == 1

    def test_revoke_last_admin_blocked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("NOVA_OIDC_ISSUER", raising=False)
        db_path = self._bootstrap_db(tmp_path)
        runner.invoke(
            app,
            ["server", "assign-role", "solo@example.com", "admin",
             "--db-path", str(db_path)],
        )
        result = runner.invoke(
            app,
            ["server", "revoke-role", "solo@example.com", "admin",
             "--db-path", str(db_path)],
        )
        # Exit code 2 = lockout invariant fired
        assert result.exit_code == 2, result.output
        assert "last admin" in result.output.lower()
