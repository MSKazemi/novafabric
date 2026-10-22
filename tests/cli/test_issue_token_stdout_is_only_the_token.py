"""L3 — ``TOKEN=$(nova server issue-token …)`` must capture the token and nothing else.

On first use (no keypair yet) the command printed "Key not found … Generating new
keypair …" and "Keypair written to …" on STDOUT, so the captured variable held
three lines and every ``Authorization: Bearer $TOKEN`` built from it was invalid.
Status lines belong on stderr; stdout is exactly one line: the JWT.
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from novafabric.cli.main import app


def _issue(key_path: Path) -> object:
    return CliRunner().invoke(
        app,
        ["server", "issue-token", "--subject", "ci@example.com", "--roles", "reader",
         "--key-path", str(key_path)],
    )


def _assert_stdout_is_one_jwt(stdout: str) -> None:
    lines = stdout.splitlines()
    assert len(lines) == 1, f"stdout must be exactly the token, got {lines!r}"
    assert lines[0].count(".") == 2


def test_first_use_keeps_keypair_messages_off_stdout(tmp_path: Path) -> None:
    key = tmp_path / "keys" / "offline.pem"
    result = _issue(key)
    assert result.exit_code == 0, result.output
    assert key.exists()
    _assert_stdout_is_one_jwt(result.stdout)
    assert "Generating new keypair" in result.stderr
    assert "Keypair written" in result.stderr


def test_second_use_stdout_is_still_only_the_token(tmp_path: Path) -> None:
    key = tmp_path / "offline.pem"
    assert _issue(key).exit_code == 0  # type: ignore[attr-defined]
    result = _issue(key)
    assert result.exit_code == 0  # type: ignore[attr-defined]
    _assert_stdout_is_one_jwt(result.stdout)  # type: ignore[attr-defined]
